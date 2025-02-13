# -*- coding: utf-8 -*-
import warnings

from django.conf import settings

import unittest
from haystack.utils import log as logging

warnings.simplefilter('ignore', Warning)


def setup():
    log = logging.getLogger('haystack')
    try:
        import elasticsearch5 as elasticsearch
        if not ((5, 0, 0) <= elasticsearch.__version__ < (6, 0, 0)):
            raise ImportError
        from elasticsearch import Elasticsearch, exceptions
    except ImportError:
        log.error("'elasticsearch>=5.0.0,<6.0.0' not installed.", exc_info=True)
        raise unittest.SkipTest("'elasticsearch>=5.0.0,<6.0.0' not installed.")

    url = settings.HAYSTACK_CONNECTIONS['default']['URL']
    es = Elasticsearch(url)
    try:
        es.info()
    except exceptions.ConnectionError as e:
        log.error("elasticsearch not running on %r" % url, exc_info=True)
        raise unittest.SkipTest("elasticsearch not running on %r" % url, e)
