import abc
import concurrent.futures
import itertools
import logging
import operator
import os
import random
import unicodedata
import urllib.parse

import appdirs
import web_cache

from sacad import http_helpers
from sacad.cover import CoverImageFormat, CoverSourceResult, CoverSourceQuality


MAX_THUMBNAIL_SIZE = 256


class CoverSource(metaclass=abc.ABCMeta):

  """ Base class for all cover sources. """

  def __init__(self, target_size, size_tolerance_prct, min_delay_between_accesses=2 / 3, allow_cookies=False):
    self.target_size = target_size
    self.size_tolerance_prct = size_tolerance_prct
    self.http = http_helpers.Http(allow_session_cookies=allow_cookies,
                                  min_delay_between_accesses=min_delay_between_accesses)
    if not hasattr(__class__, "api_cache"):
      db_filepath = os.path.join(appdirs.user_cache_dir(appname="sacad",
                                                        appauthor=False),
                                 "sacad-cache.sqlite")
      os.makedirs(os.path.dirname(db_filepath), exist_ok=True)
      day_s = 60 * 60 * 24
      __class__.api_cache = web_cache.ThreadedWebCache(db_filepath,
                                                       "cover_source_api_data",
                                                       caching_strategy=web_cache.CachingStrategy.FIFO,
                                                       expiration=random.randint(day_s * 2, day_s * 3),  # 2-3 days
                                                       compression=web_cache.Compression.DEFLATE)
      __class__.probe_cache = web_cache.ThreadedWebCache(db_filepath,
                                                         "cover_source_probe_data",
                                                         caching_strategy=web_cache.CachingStrategy.FIFO,
                                                         expiration=day_s * 30 * 6)  # 6 months
      logging.getLogger().debug("Total size of file '%s': %s" % (db_filepath,
                                                                 __class__.api_cache.getDatabaseFileSize()))
      for cache, cache_name in zip((__class__.api_cache, __class__.probe_cache),
                                   ("cover_source_api_data", "cover_source_probe_data")):
        purged_count = cache.purge()
        logging.getLogger().debug("%u obsolete entries have been removed from cache '%s'" % (purged_count, cache_name))
        row_count = len(cache)
        logging.getLogger().debug("Cache '%s' contains %u entries" % (cache_name, row_count))

  def search(self, album, artist):
    """ Search for a given album/artist and return an iterable of CoverSourceResult. """
    logging.getLogger().debug("Searching with source '%s'..." % (self.__class__.__name__))
    url_data = self.getSearchUrl(album, artist)
    if isinstance(url_data, tuple):
      url, post_data = url_data
    else:
      url = url_data
      post_data = None
    try:
      api_data = self.fetchResults(url, post_data)
      results = self.parseResults(api_data)
    except Exception as e:
      # raise
      logging.getLogger().warning("Search with source '%s' failed: %s %s" % (self.__class__.__name__,
                                                                             e.__class__.__qualname__,
                                                                             e))
      return ()

    # get metadata using thread pool
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
      futures = []
      for result in filter(operator.methodcaller("needMetadataUpdate"), results):
        futures.append(executor.submit(CoverSourceResult.updateImageMetadata, result))
      results = list(itertools.filterfalse(operator.methodcaller("needMetadataUpdate"), results))
      concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_EXCEPTION)
      # raise first exception in future if any
      for future in futures:
        try:
          e = future.exception(timeout=0)
        except concurrent.futures.TimeoutError:
          continue
        if e is not None:
          # try to stop all pending futures
          for future_to_cancel in futures:
            future_to_cancel.cancel()
          raise e
      results.extend(future.result() for future in futures)

    # filter
    results_excluded_count = 0
    reference_only_count = 0
    results_kept = []
    for result in results:
      if ((result.size[0] + (self.size_tolerance_prct * self.target_size / 100) < self.target_size) or  # skip too small images
              (result.size[1] + (self.size_tolerance_prct * self.target_size / 100) < self.target_size) or
              (result.format not in CoverImageFormat) or  # unknown format
              result.needMetadataUpdate()):  # if still true, it means we failed to grab metadata, so exclude it
        if result.source_quality is CoverSourceQuality.REFERENCE:
          # we keep this result just for the reference, it will be excluded from the results
          result.is_only_reference = True
          results_kept.append(result)
          reference_only_count += 1
        else:
          results_excluded_count += 1
      else:
        results_kept.append(result)
    result_kept_count = len(results_kept) - reference_only_count

    # log
    logging.getLogger().info("Got %u relevant (%u excluded) results from source '%s'" % (result_kept_count,
                                                                                         results_excluded_count + reference_only_count,
                                                                                         self.__class__.__name__))
    for result in itertools.filterfalse(operator.attrgetter("is_only_reference"), results_kept):
      logging.getLogger().debug("%s %s%s %4dx%4d %s%s" % (result.__class__.__name__,
                                                          ("(%02d) " % (result.rank)) if result.rank is not None else "",
                                                          result.format.name,
                                                          result.size[0],
                                                          result.size[1],
                                                          result.urls[0],
                                                          " [x%u]" % (len(result.urls)) if len(result.urls) > 1 else ""))
    return results_kept

  def fetchResults(self, url, post_data=None):
    """ Get search results froam an URL. """
    if post_data is not None:
      logging.getLogger().debug("Querying URL '%s' %s..." % (url, dict(post_data)))
    else:
      logging.getLogger().debug("Querying URL '%s'..." % (url))
    headers = {}
    self.updateHttpHeaders(headers)
    return self.http.query(url,
                           post_data=post_data,
                           headers=headers,
                           cache=__class__.api_cache)

  def probeUrl(self, url, response_headers=None):
    """ Probe URL reachability from cache or HEAD request. """
    logging.getLogger().debug("Probing URL '%s'..." % (url))
    headers = {}
    self.updateHttpHeaders(headers)
    resp_headers = {}
    resp_ok = self.http.isReachable(url,
                                    headers=headers,
                                    response_headers=resp_headers,
                                    cache=__class__.probe_cache)

    if response_headers is not None:
      response_headers.update(resp_headers)

    return resp_ok

  @staticmethod
  def assembleUrl(base_url, params):
    """ Build an URL from URL base and parameters. """
    return "%s?%s" % (base_url, urllib.parse.urlencode(params))

  @staticmethod
  def unaccentuate(s):
    """ Replace accentuated chars in string by their non accentuated equivalent. """
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

  @abc.abstractmethod
  def getSearchUrl(self, album, artist):
    """
    Build a search results URL from an album and/or artist name.

    If the URL must be accessed with an HTTP GET request, return the URL as a string.
    If the URL must be accessed with an HTTP POST request, return a tuple with:
    - the URL as a string
    - the post parameters as a collections.OrderedDict

    """
    pass

  def updateHttpHeaders(self, headers):
    """ Add API specific HTTP headers. """
    pass

  @abc.abstractmethod
  def parseResults(self, api_data):
    """ Parse API data and return an iterable of results. """
    pass
