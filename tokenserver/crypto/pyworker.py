import json
import time

from tokenserver import logger
from tokenserver.crypto.master import PROTOBUF_CLASSES

from browserid._m2_monkeypatch import DSA as _DSA
from browserid._m2_monkeypatch import RSA as _RSA
from browserid import jwt
from browserid.certificates import CertificatesManager
from browserid.tests.support import fetch_public_key

from powerhose import get_params
from mozsvc.config import Config

from tokenlib.utils import HKDF

import hashlib
from M2Crypto import BIO
import pylibmc


class MemcacheClient(pylibmc.Client):

    def __init__(self, *args, **kwargs):
        self.ttl = kwargs.pop('ttl', 0)
        super(MemcacheClient, self).__init__(*args, **kwargs)

    def set(self, key, value, time=None, *args, **kwargs):
        if time is None:
            time = self.ttl     # NOQA
        return super(MemcacheClient, self).set(str(key), value, time, *args,
                                               **kwargs)


class ExpiredValue(KeyError):
    pass


class TTLedDict(dict):
    """A simple TTLed in memory cache.

    :param ttl: the time-to-leave for records, in seconds.
                The cache will return an ExpiredValue once the TTL is over for
                its records, and remove the items from its cache.

                A ttl of 0 means the record never expires.
    """

    def __init__(self, ttl):
        self.ttl = ttl
        super(TTLedDict, self).__init__()

    def __setitem__(self, key, value):
        return super(TTLedDict, self).__setitem__(key, (time.time(), value))

    def __getitem__(self, key):
        insert_date, value = super(TTLedDict, self).__getitem__(key)
        if insert_date != 0 and insert_date + self.ttl < time.time():
            # if the ttl is expired, remove the key from the cache and return a
            # key error
            del self[key]
            raise ExpiredValue(key)
        return value

    def __contains__(self, key):
        try:
            self[key]
        except KeyError:
            return False
        else:
            return True

    def set_ttl(self, key, ttl):
        _, value = super(TTLedDict, self).__getitem__(key)
        super(TTLedDict, self).__setitem__(key, (ttl, value))


class CertificatesManagerWithCache(CertificatesManager):

    def __init__(self, memory=None, memcache=None, loadtest_mode=False):
        """If the loadtest mode is set, when looking for loadtest.local, the
        certificate bundled in browserid.tests.support will be returned instead
        of failing.

        setting :param memory: or :param memcache: to False will cause the
        certificates manager to not use them

        :param memory: the dict to use for in-memory cache
        :param memcache: the memcache instance, already configured.
        """
        if memory is None:
            memory = TTLedDict(60 * 10)  # TTL of 10 minutes for the certs

        if memcache is None:
            memcache = False

        self.memory = memory
        self.memcache = memcache
        self.loadtest_mode = loadtest_mode

        if loadtest_mode is True:
            self.memory['loadtest.local'] = fetch_public_key('loadtest.local')
            self.memory.set_ttl('loadtest.local', 0)  # never expire

    def __getitem__(self, hostname):
        """Get the certificate for the given hostname.

        If the certificate is not already in memory, try to get it in the
        shared memcache. If it's not in the the memcache, download it and store
        it both in memory and in the memcache.
        """
        hostname = str(hostname)
        if self.memory and hostname in self.memory:
            return self.memory[hostname]
        else:
            # try to get the key from memcache if it doesn't exist in memory
            if self.memcache and hostname in self.memcache:
                key = self.memcache.get(hostname)
                self.memory[hostname] = key
                return key
            else:
                # it doesn't exist in memcache either, so let's get it from
                # the issuer host.
                key = self.fetch_public_key(hostname)
                if self.memcache is not False:
                    self.memcache[hostname] = key
                if self.memory is not False:
                    self.memory[hostname] = key
                return key


def get_crypto_obj(algo, filename=None, key=None):
    if filename is None and key is None:
        raise ValueError('you need to specify either filename or key')

    if key is not None:
        bio = BIO.MemoryBuffer(str(key))
    else:
        bio = BIO.openfile(filename)

    # we can know what's the algorithm used thanks to the filename
    if algo.startswith('RS'):
        obj = _RSA.load_pub_key_bio(bio)
    elif algo.startswith('DS'):
        obj = _DSA.load_pub_key_bio(bio)
    else:
        raise ValueError('unknown algorithm')
    return obj


def get_certificate(algo, filename=None, key=None):
    obj = get_crypto_obj(algo, filename, key)
    cls = getattr(jwt, '%sKey' % algo)
    return cls(obj=obj)


class CryptoWorker(object):

    def __init__(self, certs=None, **kwargs):
        logger.info('starting a crypto worker')
        if certs is None:
            certs = CertificatesManagerWithCache(**kwargs)
        self.certs = certs

    def __call__(self, job):
        """proxy to the functions exposed by the worker"""
        logger.info('worker called with the message %s' % job)
        function_id, serialized_data = job.data.split('::', 1)
        req_cls, resp_cls = PROTOBUF_CLASSES[function_id]
        obj = req_cls()

        if not hasattr(self, function_id):
            raise ValueError('the function %s does not exists' % function_id)

        try:
            obj.ParseFromString(serialized_data)
            data = {}
            for field, value in obj.ListFields():
                data[field.name] = value
        except ValueError:
            raise ValueError('could not parse data')

        try:
            res = getattr(self, function_id)(**data)
            return resp_cls(value=res).SerializeToString()
        except BrowserIDError as e:
            return resp_cls(error_type="connection_error", error=e.message)

    def error(self, message):
        """returns an error message"""
        raise Exception(message)

    def check_signature(self, hostname, signed_data, signature, algorithm):
        data = self.certs[hostname]

        cert = jwt.load_key(algorithm, data)
        return cert.verify(signed_data, signature)

    def check_signature_with_cert(self, cert, signed_data, signature,
                                  algorithm):
        data = json.loads(cert)
        cert = jwt.load_key(algorithm, data)
        return cert.verify(signed_data, signature)

    def derivate_key(self, ikm, salt, info, l, hashmod):
        hashmod = getattr(hashlib, hashmod)
        derivated = HKDF(ikm.decode("hex"), salt.decode("hex"),
                         info.decode("hex"), l, hashmod)
        return derivated.encode("hex")


def get_crypto_worker(cls, config_file=None, **kwargs):
    """Builds a crypto worker with the given arguments.

    :param cls: the Worker class to use.
    :param config_file: the configuration file to read the values from.

    Additional keyword arguments are used to override the configuration read
    from the file. If no file is given, the keyword arguments will be used
    instead.
    """
    config = {}
    if config_file is not None:
        conf = Config(config_file)
        section = 'crypto-worker'
        # bools
        if conf.has_option(section, 'loadtest_mode'):
            config['loadtest_mode'] = bool(conf.get(section, 'loadtest_mode'))

        # ints
        for option in ('memory_ttl', 'memcache_ttl'):
            if conf.has_option(section, option):
                config[option] = conf.getint(section, option)

        # strings
        if conf.has_option(section, 'memcache_host'):
            config['memcache_host'] = conf.get(section, 'memcache_host')

    config.update(kwargs)

    mc_host = config.get('memcache_host', None)
    if mc_host is not None:
        mc_ttl = config['memcache_ttl']
        memcache = MemcacheClient((mc_host,), ttl=mc_ttl)
    else:
        memcache = False

    memory = TTLedDict(ttl=config['memory_ttl'])
    loadtest_mode = config.get('loadtest_mode', False)

    certs = CertificatesManagerWithCache(
                loadtest_mode=loadtest_mode,
                memory=memory,
                memcache=memcache)
    return cls(certs=certs)


_class = None


def crypto_worker(job, args=None):
    if args == None:
        args = get_params()
    global _class
    if _class is None:
        _class = get_crypto_worker(CryptoWorker, args['config'])
    return _class(job)
