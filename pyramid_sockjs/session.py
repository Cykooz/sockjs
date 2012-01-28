import logging
import gevent
from gevent.queue import Queue
from heapq import heappush, heappop
from datetime import datetime, timedelta
from pyramid.compat import string_types
from pyramid_sockjs.protocol import encode, decode

log = logging.getLogger('pyramid_sockjs')


class Session(object):

    new = True
    request = None
    acquired = False
    timeout = timedelta(seconds=10)

    def __init__(self, id, timeout=timedelta(seconds=10)):
        self.id = id
        self.expired = False
        self.timeout = timeout
        self.expires = datetime.now() + timeout

        self.queue = Queue()

        self.hits = 0
        self.heartbeats = 0
        self.connected = False

    def is_new(self):
        return self.new

    def __str__(self):
        result = ['id=%r' % self.id]

        if self.connected:
            result.append('connected')
        else:
            result.append('disconnected')

        if self.queue.qsize():
            result.append('queue[%s]' % self.queue.qsize())
        if self.hits:
            result.append('hits=%s' % self.hits)
        if self.heartbeats:
            result.append('heartbeats=%s' % self.heartbeats)

        return ' '.join(result)

    def tick(self, timeout=None):
        self.expired = False

        if timeout is None:
            self.expires = datetime.now() + self.timeout
        else:
            self.expires = datetime.now() + timeout

    def heartbeat(self):
        self.heartbeats += 1

    def expire(self):
        """ Manually expire a session. """
        self.expired = True
        self.connected = False

    def open(self):
        log.info('open session: %s', self.id)
        self.new = False
        self.connected = True
        try:
            self.on_open()
        except:
            log.exception("Exceptin in .on_open method.")

    def acquire(self, request=None):
        self.manager.acquire(self, request)

    def release(self):
        self.manager.release(self)

    def get_transport_message(self, block=True, timeout=None):
        self.tick()
        return self.queue.get(block=block, timeout=timeout)

    def send(self, msg):
        log.info('outgoing message: %s, %s', self.id, msg)
        self.tick()
        self.queue.put_nowait(msg)

    def message(self, msg):
        log.info('incoming message: %s, %s', self.id, msg)
        self.tick()
        try:
            self.on_message(msg)
        except:
            log.exception("Exceptin in .on_message method.")

    def close(self):
        log.info('close session: %s', self.id)
        self.expire()
        try:
            self.on_close()
        except:
            log.exception("Exceptin in .on_message method.")

    def on_open(self):
        """ override in subsclass """

    def on_message(self, msg):
        """ override in subsclass """

    def on_close(self):
        """ override in subsclass """


class SessionManager(dict):
    """ A basic session manager """

    _gc_thread = None
    _gc_thread_stop = False

    def __init__(self, name, registry, session=Session,
                 gc_cycle=3.0, timeout=timedelta(seconds=10)):
        self.name = name
        self.route_name = 'sockjs-url-%s'%name
        self.registry = registry
        self.factory = session
        self.acquired = {}
        self.pool = []
        self.timeout = timeout
        self._gc_cycle = gc_cycle

    def route_url(self, request):
        return request.route_url(self.route_name)

    def start(self):
        if self._gc_thread is None:
            def _gc_sessions():
                while not self._gc_thread_stop:
                    gevent.sleep(self._gc_cycle)
                    self._gc() # pragma: no cover

            self._gc_thread = gevent.Greenlet(_gc_sessions)

        if not self._gc_thread:
            self._gc_thread.start()

    def stop(self):
        if self._gc_thread:
            self._gc_thread_stop = True
            self._gc_thread.join()

    def _gc(self):
        current_time = datetime.now()

        while self.pool:
            expires, session = self.pool[0]

            # check if session is removed
            if session.id in self:
                if expires > current_time:
                    break
            else:
                self.pool.pop(0)
                continue

            expires, session = self.pool.pop(0)

            # Session is to be GC'd immedietely
            if session.expires < current_time:
                del self[session.id]
                if session.id in self.acquired:
                    del self.acquired[session.id]
                if session.connected:
                    session.close()
            else:
                heappush(self.pool, (session.expires, session))

    def _add(self, session):
        if session.expired:
            raise ValueError("Can't add expired session")

        session.manager = self
        session.registry = self.registry

        self[session.id] = session
        heappush(self.pool, (session.expires, session))

    def get(self, id, create=False):
        session = super(SessionManager, self).get(id, None)
        if session is None:
            if create:
                session = self.factory(id, self.timeout)
                self._add(session)
            else:
                raise KeyError(id)

        return session

    def acquire(self, session, request=None):
        sid = session.id

        if sid in self.acquired:
            raise KeyError("Another connection still open")
        if sid not in self:
            raise KeyError("Unknown session")

        session.tick()
        session.hits += 1
        session.request = request
        self.acquired[sid] = True
        return session

    def is_acquired(self, session):
        return session.id in self.acquired

    def release(self, session):
        session.request = None
        if session.id in self.acquired:
            del self.acquired[session.id]

        session.request = None

    def active_sessions(self):
        for session in self.values():
            if not session.expired:
                yield session

    def clear(self):
        """ Manually expire all sessions in the pool. """
        while self.pool:
            expr, session = heappop(self.pool)
            session.expire()
            del self[session.id]

    def broadcast(self, msg):
        for session in self.values():
            if not session.expired:
                session.send(msg)

    def __del__(self):
        self.clear()
        self.stop()