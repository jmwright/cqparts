#!/usr/bin/env python

import os
import inspect
import scrapy
import scrapy.crawler
import scrapy.exporters
import re
import argparse
import logging
import fnmatch
import json
import csv


# ---------- Utilities ----------

def split_url(url):
    match = re.search(r'^(?P<base>.*)\?(?P<params>.*)$', url, flags=re.I)
    return (
        match.group('base'),
        {k: v for (k, v) in (p.split('=') for p in match.group('params').split('&'))}
    )

def join_url(base, params):
    return "{base}?{params}".format(
        base=base,
        params='&'.join('%s=%s' % (k, v) for (k, v) in params.items()),
    )


# ---------- Scraper Spiders ----------

class BoltDepotSpider(scrapy.Spider):
    FEED_URI = "%(prefix)sscrape-%(name)s.json"


class BoltDepotProductSpider(BoltDepotSpider):

    def parse(self, response):
        # Look for : Product catalogue table
        product_catalogue = response.css('table.product-catalog-table')
        if product_catalogue:
            for catalogue_link in product_catalogue.css('li a'):
                next_page_url = catalogue_link.css('::attr("href")').extract_first()
                yield response.follow(next_page_url, self.parse)

        # Look for : Product list table
        product_list = response.css('#product-list-table')
        if product_list:
            for product in product_list.css('td.cell-prod-no'):
                next_page_url = product.css('a::attr("href")').extract_first()
                yield response.follow(next_page_url, self.parse_product_detail)

    def parse_product_detail(self, response):
        heading = response.css('#catalog-header-title h1::text').extract_first()
        print("Product: %s" % heading)

        (url_base, url_params) = split_url(response.url)

        # details table
        detail_table = response.css('#product-property-list')
        details = {}
        for row in detail_table.css('tr'):
            key = row.css('td.name span::text').extract_first()
            value = row.css('td.value span::text').extract_first()
            if key and value:
                details[key] = value

        product_data = {
            'id': url_params['product'],
            'name': heading,
            'url': response.url,
            'details': details,
        }

        # Image url
        image_url = response.css('.catalog-header-product-image::attr("src")').extract_first()
        if image_url:
            product_data.update({'image_url': image_url})

        yield product_data


class WoodScrewSpider(BoltDepotProductSpider):
    name = 'woodscrews'
    start_urls = [
        'https://www.boltdepot.com/Wood_screws_Phillips_flat_head.aspx',
        'https://www.boltdepot.com/Wood_screws_Slotted_flat_head.aspx',
    ]


class BoltSpider(BoltDepotProductSpider):
    name = 'bolts'
    start_urls = [
        'https://www.boltdepot.com/Hex_bolts_2.aspx',
        'https://www.boltdepot.com/Metric_hex_bolts_2.aspx',
    ]


class NutSpider(BoltDepotProductSpider):
    name = 'nuts'
    start_urls = [
        'https://www.boltdepot.com/Hex_nuts.aspx',
        'https://www.boltdepot.com/Square_nuts.aspx',
        'https://www.boltdepot.com/Metric_hex_nuts.aspx',
    ]


class ThreadedRodSpider(BoltDepotProductSpider):
    name = 'threaded-rods'
    start_urls = [
        'https://www.boltdepot.com/Threaded_rod.aspx',
        'https://www.boltdepot.com/Metric_threaded_rod.aspx',
    ]


SPIDERS = [
    WoodScrewSpider,
    BoltSpider,
    NutSpider,
    ThreadedRodSpider,
]
SPIDER_MAP = {
    cls.name: cls
    for cls in SPIDERS
}


class GrowingList(list):
    """
    A list that will automatically expand if indexed beyond its limit.
    (the list equivalent of collections.defaultdict)
    """

    def __init__(self, *args, **kwargs):
        self._default_type = kwargs.pop('default_type', lambda: None)
        super(GrowingList, self).__init__(*args, **kwargs)

    def __getitem__(self, index):
        if index >= len(self):
            self.extend([self._default_type() for i in range(index + 1 - len(self))])
        return super(GrowingList, self).__getitem__(index)

    def __setitem__(self, index, value):
        if index >= len(self):
            self.extend([self._default_type() for i in range(index + 1 - len(self))])
        super(GrowingList, self).__setitem__(index, value)


class BoltDepotDataSpider(BoltDepotSpider):

    @staticmethod
    def table_data(table):
        # Pull data out of a table into a 2d list.
        # Merged Cells:
        #   any merged cells (using rowspan / colspan) will have duplicate
        #   data over each cell.
        #   "merging cells does not a database make" said me, just now

        def push_data(row, i, val):
            # push data into next available slot in the given list
            # return the index used (will be >= i)
            assert isinstance(row, GrowingList), "%r" % row
            assert val is not None
            try:
                while row[i] is not None:
                    i += 1
            except IndexError:
                pass
            row[i] = val
            return i

        data = GrowingList(default_type=GrowingList)  # nested growing list
        header_count = 0
        for (i, row) in enumerate(table.css('tr')):
            j = 0
            is_header_row = True
            for cell in row.css('th, td'):
                # cell data
                value = cell.css('::text').extract_first()
                if value is None:
                    value = ''
                value = value.rstrip('\r\n\t ')
                rowspan = int(cell.css('::attr("rowspan")').extract_first() or 1)
                colspan = int(cell.css('::attr("colspan")').extract_first() or 1)
                # is header row?
                if cell.root.tag != 'th':
                    is_header_row = False
                # populate data (duplicate merged content)
                j = push_data(data[i], j, value)
                for di in range(rowspan):
                    for dj in range(colspan):
                        data[i + di][j + dj] = value
                j += 1
            if is_header_row:
                header_count += 1

        return (data, header_count)

    def parse(self, response):
        table = response.css('table.fastener-info-table')
        (data, header_count) = self.table_data(table)

        header = data[header_count - 1]  # last header row
        for row in data[header_count:]:
            row_data = dict(zip(header, row))
            if any(v for v in row_data.values()):
                # don't yield if there's no data
                yield row_data


class DataWoodScrewDiam(BoltDepotDataSpider):
    name = 'd-woodscrew-diam'
    start_urls = [
        'https://www.boltdepot.com/fastener-information/Wood-Screws/Wood-Screw-Diameter.aspx',
    ]

class DataUSBoltThreadLen(BoltDepotDataSpider):
    name = 'd-us-bolt-thread-len'
    start_urls = [
        'https://www.boltdepot.com/fastener-information/Bolts/US-Thread-Length.aspx',
    ]

class DataUSThreadPerInch(BoltDepotDataSpider):
    name = 'd-us-tpi'
    start_urls = [
        'https://www.boltdepot.com/fastener-information/Measuring/US-TPI.aspx',
    ]

class DataMetricThreadPitch(BoltDepotDataSpider):
    name = 'd-met-threadpitch'
    start_urls = [
        'https://www.boltdepot.com/fastener-information/Measuring/Metric-Thread-Pitch.aspx',
    ]

class DataMetricBoltHeadSize(BoltDepotDataSpider):
    name = 'd-met-boltheadsize'
    start_urls = [
        'https://www.boltdepot.com/fastener-information/Bolts/Metric-Bolt-Head-Size.aspx',
    ]


METRICS_SPIDERS = [
    DataWoodScrewDiam,
    DataUSBoltThreadLen,
    DataUSThreadPerInch,
    DataMetricThreadPitch,
    DataMetricBoltHeadSize,
]

# ---------- Command-line Arguments Parser ----------
DEFAULT_PREFIX = os.path.splitext(os.path.basename(
    os.path.abspath(inspect.getfile(inspect.currentframe()))
))[0] + '-'

parser = argparse.ArgumentParser(
    description='Build Bolt Depot catalogue by crawling their website',
    epilog="""
Actions:
    scrape  scrape product details from website
    csv     convert scraped output to csv [optional]
    build   builds catalogue from scraped data

Note: Actions will always be performed in the order shown above,
      even if they're not listed in that order on commandline.
    """,
    formatter_class=argparse.RawTextHelpFormatter,
)

VALID_ACTIONS = set(['scrape', 'csv', 'build'])
def action_type(value):
    value = value.lower()
    if value not in VALID_ACTIONS:
        raise argparse.ArgumentError()
    return value

parser.add_argument(
    'actions', metavar='action', type=action_type, nargs='*',
    help='action(s) to perform'
)

# Scraper arguments
parser.add_argument(
    '--prefix', '-p', dest='prefix', default=DEFAULT_PREFIX,
    help="scraper file prefix (default: '%s')" % DEFAULT_PREFIX,
)

parser.add_argument(
    '--onlymetrics', '-om', dest='onlymetrics',
    action='store_const', const=True, default=False,
    help="if set, when scraping, only metrics data is scraped"
)

# Catalogues
parser.add_argument(
    '--list', '-l', dest='list',
    default=False, action='store_const', const=True,
    help="list catalogues to build",
)

def catalogues_list_type(value):
    catalogues_all = set(SPIDER_MAP.keys())
    catalogues = set()
    for filter_str in value.split(','):
        catalogues |= set(fnmatch.filter(catalogues_all, filter_str))
    return sorted(catalogues)

parser.add_argument(
    '--catalogues', '-c', dest='catalogues',
    type=catalogues_list_type, default=catalogues_list_type('*'),
    help="csv list of catalogues to act on",
)

args = parser.parse_args()

BoltDepotSpider.prefix = args.prefix


# list catalogues & exit
if args.list:
    for name in args.catalogues:
        print(name)
    exit(0)

# no actions, print help & exit
if not args.actions:
    parser.print_help()
    exit(1)


# ----- Start Crawl -----

if 'scrape' in args.actions:
    print("----- Scrape: %s (+ metrics)" % (', '.join(args.catalogues)))

    # --- Clear feed files
    feed_names = []
    if not args.onlymetrics:
        feed_names += args.catalogues
    feed_names += [cls.name for cls in METRICS_SPIDERS]

    for name in feed_names:
        feed_filename = BoltDepotSpider.FEED_URI % {
            'prefix': args.prefix, 'name': name,
        }
        if os.path.exists(feed_filename):
            os.unlink(feed_filename)  # remove feed file to populate from scratch


    # --- Create Crawlers
    process = scrapy.crawler.CrawlerProcess(
        settings={
            'LOG_LEVEL': logging.INFO,
            'FEED_FORMAT': "json",
            'FEED_URI': BoltDepotSpider.FEED_URI,
        },
    )

    # product crawlers
    if not args.onlymetrics:
        for name in args.catalogues:
            process.crawl(SPIDER_MAP[name])
    # metrics crawlers
    for metrics_spider in METRICS_SPIDERS:
        process.crawl(metrics_spider)

    # --- Start Scraping
    process.start()


# ----- Convert to CSV -----

if 'csv' in args.actions:
    for name in args.catalogues:
        print("----- CSV: %s" % name)
        feed_json = FEED_URI % {
            'prefix': args.prefix, 'name': name,
        }
        with open(feed_json, 'r') as json_file:
            data = json.load(json_file)

        # Pull out headers
        KEYS = set(['url', 'image_url', 'name', 'id'])
        headers = set(KEYS)
        for item in data:
            headers |= set(item['details'].keys())

        # Write Output
        def utf8encoded(d):
            return {k.encode('utf-8'): v.encode('utf-8') for (k, v) in d.items()}
        feed_csv = "%s.csv" % os.path.splitext(feed_json)[0]
        with open(feed_csv, 'w') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=headers)
            writer.writeheader()
            for item in data:
                row_data = utf8encoded(item['details'])
                row_data.update({k: item[k] for k in KEYS})
                writer.writerow(row_data)


# ----- Build Catalogues -----

def build_screw(row):

    # Required Parameters:
    #   - drive
    #       -
    #   - head
    #       - <countersunk>
    #       - <>
    #   - thread <triangular>
    #       - diameter
    #       - diameter_core (defaults to 2/3 diameter)
    #       - pitch
    #       - angle (defaults to 30deg)
    #   - length
    #   - neck_diam
    #   - neck_length
    #   - neck_taper
    #   - tip_diameter
    #   - tip_length

    pass


if 'build' in args.actions:
    #for name in args.catalogues:
    #    print("----- Build: %s" % name)
    print("=================== WORK IN PROGRESS ===================")
    raise NotImplementedError("I'm getting there")
    pass
