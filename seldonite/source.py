import logging
import os

from seldonite.model import Article
from seldonite.helpers import filter, heuristics, utils

from newsplease.crawler import commoncrawl_extractor, commoncrawl_crawler
from newsplease.helper_classes.heuristics import Heuristics
from googleapiclient.discovery import build as gbuild

# TODO make abstract
class Source:
    '''
    Base class for a source

    A source can be anything from a search engine, to an API, to a dataset
    '''

    # TODO make abstract
    def __init__(self):
        # flag to show this source returns in a timely fashion without callbacks, unless overriden
        self.uses_callback = False
        self.can_keyword_filter = False
        # we need to filter for only news articles by default
        self.news_only = False

    def set_date_range(self, start_date, end_date, strict=True):
        '''
        params:
        start_date: (if None, any date is OK as start date), as datetime
        end_date: (if None, any date is OK as end date), as datetime
        strict: if date filtering is strict and the date of an article could not be detected, the article will be discarded
        '''
        self.start_date = start_date
        self.end_date = end_date
        self.strict = strict

    def fetch(self):
        articles = self._fetch()

        for article in articles:
            if self.news_only:
                yield article
            # apply newsplease heuristics to get only articles
            else:
                if heuristics.og_type(article):
                    yield article

    def _fetch(self):
        raise NotImplementedError()

class WebWideSource(Source):
    '''
    Parent class for web wide sources
    '''

    def __init__(self, hosts=[]):
        '''
        params:
        hosts: If None or empty list, any host is OK. Example: ['cbc.ca']
        '''
        super().__init__()

        self.hosts = hosts
        self.keywords = []

    def set_keywords(self, keywords=[]):
        self.keywords = keywords

class CommonCrawlWithNewsPlease(WebWideSource):
    '''
    Source that uses the news-please library to search CommonCrawl
    '''

    def __init__(self, store_path, hosts=[]):
        '''
        params:
        store_path: Path to directory where downloaded files will be kept
        '''
        super().__init__(hosts)

        raise NotImplemented('Not fully implemented!')

        self.store_path = store_path

        # flag to show this source works via callbacks due to long running process
        self.uses_callback = True
        self.can_keyword_filter = True


    def _fetch(self, collector_cb):

        # keep the collector_cb for this class cb to call
        self.collector_cb = collector_cb

        # create place for warc files
        warc_dir_path = os.path.join(self.store_path, 'cc_download_warc')
        if not os.path.exists(warc_dir_path):
            os.makedirs(warc_dir_path)

        # filter by keyword as early as possible
        if self.keywords:
            keywords = self.keywords

            # create a class for newsplease lib that can filter by keyword
            class FilterExtractorClass(commoncrawl_extractor.CommonCrawlExtractor):
                def filter_record(self, warc_record, article=None):
                    keep, article = super().filter_record(warc_record, article=article)

                    if not keep:
                        return keep, article

                    if filter.contains_keywords(article, keywords):
                        return True, article
                    else:
                        return False, article

            custom_extractor_cls = FilterExtractorClass
        else:
            custom_extractor_cls = commoncrawl_extractor.CommonCrawlExtractor


        commoncrawl_crawler.crawl_from_commoncrawl(self.article_cb,
                                                   valid_hosts=self.hosts,
                                                   start_date=self.start_date,
                                                   end_date=self.end_date,
                                                   strict_date=self.strict,
                                                   # if True, the script checks whether a file has been downloaded already and uses that file instead of downloading
                                                   # again. Note that there is no check whether the file has been downloaded completely or is valid!
                                                   reuse_previously_downloaded_files=True,
                                                   local_download_dir_warc=warc_dir_path,
                                                   continue_after_error=True,
                                                   show_download_progress=True,
                                                   number_of_extraction_processes=1,
                                                   log_level=logging.INFO,
                                                   delete_warc_after_extraction=False,
                                                   # if True, will continue extraction from the latest fully downloaded but not fully extracted WARC files and then
                                                   # crawling new WARC files. This assumes that the filter criteria have not been changed since the previous run!
                                                   continue_process=False,
                                                   fetch_images=False,
                                                   extractor_cls=custom_extractor_cls)

    def article_cb(self, article):
        '''
        Convert newsplease article to seldonite article and send to collector
        '''
        self.collector_cb(article)

class SearchEngineSource(WebWideSource):

    # TODO this is incorrect syntax for param expansion, fix
    def __init__(self, hosts):
        super().__init__(hosts)

        self.can_keyword_filter = True

        

class Google(SearchEngineSource):
    '''
    Source that uses Google's Custom Search JSON API
    '''

    def __init__(self, dev_key, engine_id, hosts=[], limit_request=False):
        super().__init__(hosts)

        self.dev_key = dev_key
        self.engine_id = engine_id
        self.limit_request = limit_request

    def _fetch(self):

        service = gbuild("customsearch", "v1",
            developerKey=self.dev_key)

        # construct keywords into query
        query = ' '.join(self.keywords)

        # using siterestrict allows more than 10000 calls per day
        # note both methods still require payment for more than 100 requests a day
        if self.hosts:
            method = service.cse()
        else:
            method = service.cse().siterestrict()

        # google custom search returns max of 100 results
        # each page contains max 10 results

        num_pages = 10 if not self.limit_request else 1

        # TODO add hosts to query
        for page_num in range(num_pages):
            results = method.list(
                q=query,
                cx=self.engine_id,
                start=str((page_num * 10) + 1)
            ).execute()

            items = results['items']

            for item in items:
                link = item['link']
                yield utils.link_to_article(link)


class Bing(SearchEngineSource):
    def __init__(self):
        raise NotImplementedError()