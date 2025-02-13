# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function, unicode_literals

import datetime

import haystack
from django.conf import settings
from haystack.backends import BaseEngine
from haystack.constants import DEFAULT_OPERATOR, DJANGO_CT
from haystack.exceptions import MissingDependency
from haystack.utils import get_identifier, get_model_ct

from haystack_elasticsearch.constants import FUZZINESS
from haystack_elasticsearch.elasticsearch import ElasticsearchSearchBackend, ElasticsearchSearchQuery

try:
    import elasticsearch5 as elasticsearch
    if not ((5, 0, 0) <= elasticsearch.__version__ < (6, 0, 0)):
        raise ImportError
    from elasticsearch5.helpers import bulk, scan
except ImportError:
    raise MissingDependency("The 'elasticsearch5' backend requires the \
                            installation of 'elasticsearch>=5.0.0,<6.0.0'. \
                            Please refer to the documentation.")


class Elasticsearch5SearchBackend(ElasticsearchSearchBackend):
    def __init__(self, connection_alias, **connection_options):
        super(Elasticsearch5SearchBackend, self).__init__(connection_alias, **connection_options)
        self.content_field_name = None

    def clear(self, models=None, commit=True):
        """
        Clears the backend of all documents/objects for a collection of models.

        :param models: List or tuple of models to clear.
        :param commit: Not used.
        """
        if models is not None:
            assert isinstance(models, (list, tuple))

        try:
            if models is None:
                self.conn.indices.delete(index=self.index_name, ignore=404)
                self.setup_complete = False
                self.existing_mapping = {}
                self.content_field_name = None
            else:
                models_to_delete = []

                for model in models:
                    models_to_delete.append("%s:%s" % (DJANGO_CT, get_model_ct(model)))

                # Delete using scroll API
                query = {'query': {'query_string': {'query': " OR ".join(models_to_delete)}}}
                generator = scan(self.conn, query=query, index=self.index_name, doc_type='modelresult')
                actions = ({
                    '_op_type': 'delete',
                    '_id': doc['_id'],
                } for doc in generator)
                bulk(self.conn, actions=actions, index=self.index_name, doc_type='modelresult')
                self.conn.indices.refresh(index=self.index_name)

        except elasticsearch.TransportError as e:
            if not self.silently_fail:
                raise

            if models is not None:
                self.log.error("Failed to clear Elasticsearch index of models '%s': %s",
                               ','.join(models_to_delete), e, exc_info=True)
            else:
                self.log.error("Failed to clear Elasticsearch index: %s", e, exc_info=True)

    def _build_search_kwargs_default(self, content_field, query_string):
        if query_string == '*:*':
            return {
                'query': {
                    "match_all": {}
                },
            }
        else:
            return {
                'query': {
                    'query_string': {
                        'default_field': content_field,
                        'default_operator': DEFAULT_OPERATOR,
                        'query': query_string,
                        'analyze_wildcard': True,
                        'auto_generate_phrase_queries': True,
                        'fuzziness': FUZZINESS,
                    },
                },
            }

    def _build_search_kwargs_fields(self, fields):
        if fields:
            if isinstance(fields, (list, set)):
                fields = " ".join(fields)

        return 'stored_fields', fields

    def _build_search_kwargs_highlight(self, content_field, highlight):
        result = {
            'fields': {
                content_field: {},
            }
        }
        if isinstance(highlight, dict):
            result.update(highlight)
        return result

    def _build_search_kwargs_facets(self, facets, date_facets,query_facets):
        result = {}

        if facets is not None:
            index = haystack.connections[self.connection_alias]\
                .get_unified_index()
            for facet_fieldname, extra_options in facets.items():
                facet_options = {
                    'meta': {
                        '_type': 'terms',
                    },
                    'terms': {
                        'field': index.get_facet_fieldname(facet_fieldname),
                    }
                }
                if 'order' in extra_options:
                    facet_options['meta']['order'] = extra_options.pop('order')
                # Special cases for options applied at the facet level
                # (not the terms level).
                if extra_options.pop('global_scope', False):
                    # Renamed "global_scope" since "global" is a python keyword.
                    facet_options['global'] = True
                if 'facet_filter' in extra_options:
                    facet_options['facet_filter'] = extra_options.pop(
                        'facet_filter')
                facet_options['terms'].update(extra_options)
                result[facet_fieldname] = facet_options

        if date_facets is not None:
            for facet_fieldname, value in date_facets.items():
                # Need to detect on gap_by & only add amount if it's more than one.
                interval = value.get('gap_by').lower()

                # Need to detect on amount (can't be applied on months or years).
                if value.get('gap_amount', 1) != 1 and interval not in ('month', 'year'):
                    # Just the first character is valid for use.
                    interval = "%s%s" % (value['gap_amount'], interval[:1])

                result[facet_fieldname] = {
                    'meta': {
                        '_type': 'date_histogram',
                    },
                    'date_histogram': {
                        'field': facet_fieldname,
                        'interval': interval,
                    },
                    'aggs': {
                        facet_fieldname: {
                            'date_range': {
                                'field': facet_fieldname,
                                'ranges': [
                                    {
                                        'from': self._from_python(
                                            value.get('start_date')),
                                        'to': self._from_python(
                                            value.get('end_date')),
                                    }
                                ]
                            }
                        }
                    }
                }

        if query_facets is not None:
            for facet_fieldname, value in query_facets:
                result[facet_fieldname] = {
                    'meta': {
                        '_type': 'query',
                    },
                    'filter': {
                        'query_string': {
                            'query': value,
                        }
                    },
                }

        return 'aggs', result

    def _build_search_filters_narrow_query(self, q):
        return {
            'query_string': {
                'query': q
            }
        }

    def _build_search_kwargs_query(self, filters, query):
        if filters:
            result = {"bool": {"must": query}}
            if len(filters) == 1:
                result['bool']["filter"] = filters[0]
            else:
                result['bool']["filter"] = {"bool": {"must": filters}}
        return result

    def more_like_this(self, model_instance, additional_query_string=None,
                       start_offset=0, end_offset=None, models=None,
                       limit_to_registered_models=None, result_class=None, **kwargs):
        from haystack import connections

        if not self.setup_complete:
            self.setup()

        # Deferred models will have a different class ("RealClass_Deferred_fieldname")
        # which won't be in our registry:
        model_klass = model_instance._meta.concrete_model

        index = connections[self.connection_alias].get_unified_index().get_index(model_klass)
        field_name = index.get_content_field()
        params = {}

        if start_offset is not None:
            params['from_'] = start_offset

        if end_offset is not None:
            params['size'] = end_offset - start_offset

        doc_id = get_identifier(model_instance)

        try:
            # More like this Query
            # https://www.elastic.co/guide/en/elasticsearch/reference/2.2/query-dsl-mlt-query.html
            mlt_query = {
                'query': {
                    'more_like_this': {
                        'fields': [field_name],
                        'like': [{
                            "_id": doc_id
                        }]
                    }
                }
            }

            narrow_queries = []

            if additional_query_string and additional_query_string != '*:*':
                additional_filter = {
                    "query_string": {
                        "query": additional_query_string
                    }
                }
                narrow_queries.append(additional_filter)

            if limit_to_registered_models is None:
                limit_to_registered_models = getattr(settings, 'HAYSTACK_LIMIT_TO_REGISTERED_MODELS', True)

            if models and len(models):
                model_choices = sorted(get_model_ct(model) for model in models)
            elif limit_to_registered_models:
                # Using narrow queries, limit the results to only models handled
                # with the current routers.
                model_choices = self.build_models_list()
            else:
                model_choices = []

            if len(model_choices) > 0:
                model_filter = {"terms": {DJANGO_CT: model_choices}}
                narrow_queries.append(model_filter)

            if len(narrow_queries) > 0:
                mlt_query = {
                    "query": {
                        "bool": {
                            'must': mlt_query['query'],
                            'filter': {
                                'bool': {
                                    'must': list(narrow_queries)
                                }
                            }
                        }
                    }
                }

            raw_results = self.conn.search(
                body=mlt_query,
                index=self.index_name,
                doc_type='modelresult',
                _source=True, **params)
        except elasticsearch.TransportError as e:
            if not self.silently_fail:
                raise

            self.log.error("Failed to fetch More Like This from Elasticsearch for document '%s': %s",
                           doc_id, e, exc_info=True)
            raw_results = {}

        return self._process_results(raw_results, result_class=result_class)

    def _process_results(self, raw_results, highlight=False,
                         result_class=None, distance_point=None,
                         geo_sort=False):
        results = super(Elasticsearch5SearchBackend, self)._process_results(raw_results, highlight,
                                                                            result_class, distance_point,
                                                                            geo_sort)
        facets = {}
        if 'aggregations' in raw_results:
            facets = {
                'fields': {},
                'dates': {},
                'queries': {},
            }

            for facet_fieldname, facet_info in raw_results['aggregations'].items():
                facet_type = facet_info['meta']['_type']
                if facet_type == 'terms':
                    facets['fields'][facet_fieldname] = [(individual['key'], individual['doc_count']) for individual in facet_info['buckets']]
                    if 'order' in facet_info['meta']:
                        if facet_info['meta']['order'] == 'reverse_count':
                            srt = sorted(facets['fields'][facet_fieldname], key=lambda x: x[1])
                            facets['fields'][facet_fieldname] = srt
                elif facet_type == 'date_histogram':
                    # Elasticsearch provides UTC timestamps with an extra three
                    # decimals of precision, which datetime barfs on.
                    facets['dates'][facet_fieldname] = [(datetime.datetime.utcfromtimestamp(individual['key'] / 1000), individual['doc_count']) for individual in facet_info['buckets']]
                elif facet_type == 'query':
                    facets['queries'][facet_fieldname] = facet_info['doc_count']
        results['facets'] = facets
        return results


class Elasticsearch5SearchQuery(ElasticsearchSearchQuery):
    def add_field_facet(self, field, **options):
        """Adds a regular facet on a field."""
        # to be renamed to the facet fieldname by build_search_kwargs later
        self.facets[field] = options.copy()


class Elasticsearch5SearchEngine(BaseEngine):
    backend = Elasticsearch5SearchBackend
    query = Elasticsearch5SearchQuery
