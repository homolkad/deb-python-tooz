# -*- coding: utf-8 -*-

#    Copyright (C) 2014 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import errno
import os

import futurist
from futurist import waiters
import msgpack
from oslo_serialization import msgpackutils
from oslo_utils import encodeutils
import six

from tooz import coordination


class ProxyExecutor(object):
    KIND_TO_FACTORY = {
        'threaded': (lambda:
                     futurist.ThreadPoolExecutor(max_workers=1)),
        'greenthreaded': (lambda:
                          futurist.GreenThreadPoolExecutor(max_workers=1)),
        'synchronous': lambda: futurist.SynchronousExecutor(),
    }

    # Provide a few common aliases...
    KIND_TO_FACTORY['thread'] = KIND_TO_FACTORY['threaded']
    KIND_TO_FACTORY['threading'] = KIND_TO_FACTORY['threaded']
    KIND_TO_FACTORY['sync'] = KIND_TO_FACTORY['synchronous']
    KIND_TO_FACTORY['greenthread'] = KIND_TO_FACTORY['greenthreaded']
    KIND_TO_FACTORY['greenthreading'] = KIND_TO_FACTORY['greenthreaded']

    DEFAULT_KIND = 'threaded'

    def __init__(self, driver_name, default_executor_factory, executor=None):
        self.default_executor_factory = default_executor_factory
        self.driver_name = driver_name
        self.dispatched = set()
        self.started = False
        if executor is None:
            self.executor = None
            self.internally_owned = True
        else:
            self.executor = executor
            self.internally_owned = False

    @classmethod
    def build(cls, driver_name, options):
        default_executor_fact = cls.KIND_TO_FACTORY[cls.DEFAULT_KIND]
        executor = None
        if 'executor' in options:
            executor = options['executor']
            if isinstance(executor, six.string_types):
                try:
                    default_executor_fact = cls.KIND_TO_FACTORY[executor]
                    executor = None
                except KeyError:
                    executors_known = sorted(list(cls.KIND_TO_FACTORY))
                    raise coordination.ToozError("Unknown executor string"
                                                 " '%s' accepted values"
                                                 " are %s" % (executor,
                                                              executors_known))
        return cls(driver_name, default_executor_fact, executor=executor)

    def start(self):
        if self.started:
            return
        if self.internally_owned:
            self.executor = self.default_executor_factory()
        self.started = True

    def stop(self):
        self.started = False
        not_done = self.dispatched.copy()
        if not_done:
            waiters.wait_for_all(not_done)
        if self.internally_owned:
            executor = self.executor
            self.executor = None
            if executor is not None:
                executor.shutdown()

    def _on_done(self, fut):
        self.dispatched.discard(fut)

    def submit(self, cb, *args, **kwargs):
        if not self.started:
            raise coordination.ToozError("%s driver asynchronous executor"
                                         " has not been started"
                                         % self.driver_name)
        try:
            fut = self.executor.submit(cb, *args, **kwargs)
        except RuntimeError:
            raise coordination.ToozError("%s driver asynchronous executor has"
                                         " been shutdown" % self.driver_name)
        else:
            self.dispatched.add(fut)
            fut.add_done_callback(self._on_done)
            return fut


def safe_abs_path(rooted_at, *pieces):
    # Avoids the following junk...
    #
    # >>> import os
    # >>> os.path.join("/b", "..")
    # '/b/..'
    # >>> os.path.abspath(os.path.join("/b", ".."))
    # '/'
    path = os.path.abspath(os.path.join(rooted_at, *pieces))
    if not path.startswith(rooted_at):
        raise ValueError("Unable to create path that is outside of"
                         " parent directory '%s' using segments %s"
                         % (rooted_at, list(pieces)))
    return path


def ensure_tree(path):
    """Create a directory (and any ancestor directories required).

    :param path: Directory to create
    """
    try:
        os.makedirs(path)
    except EnvironmentError as e:
        if e.errno != errno.EEXIST or not os.path.isdir(path):
            raise
        return False
    else:
        return True


def collapse(config, exclude=None, item_selector=None):
    """Collapses config with keys and **list/tuple** values.

    NOTE(harlowja): The last item/index from the list/tuple value is selected
    be default as the new value (values that are not lists/tuples are left
    alone). If the list/tuple value is empty (zero length), then no value
    is set.
    """
    if not isinstance(config, dict):
        raise TypeError("Unexpected config type, dict expected")
    if not config:
        return {}
    if exclude is None:
        exclude = set()
    if item_selector is None:
        item_selector = lambda items: items[-1]
    collapsed = {}
    for (k, v) in six.iteritems(config):
        if isinstance(v, (tuple, list)):
            if k in exclude:
                collapsed[k] = v
            else:
                if len(v):
                    collapsed[k] = item_selector(v)
        else:
            collapsed[k] = v
    return collapsed


# TODO(harlowja): get rid of this...
#: Return the string (unicode) representation of an exception.
exception_message = encodeutils.exception_to_unicode


def to_binary(text, encoding='ascii'):
    """Return the binary representation of string (if not already binary)."""
    if not isinstance(text, six.binary_type):
        text = text.encode(encoding)
    return text


def dumps(data, excp_cls=coordination.SerializationError):
    """Serializes provided data using msgpack into a byte string."""
    try:
        return msgpackutils.dumps(data)
    except (msgpack.PackException, ValueError) as e:
        coordination.raise_with_cause(excp_cls, exception_message(e),
                                      cause=e)


def loads(blob, excp_cls=coordination.SerializationError):
    """Deserializes provided data using msgpack (from a prior byte string)."""
    try:
        return msgpackutils.loads(blob)
    except (msgpack.UnpackException, ValueError) as e:
        coordination.raise_with_cause(excp_cls, exception_message(e),
                                      cause=e)
