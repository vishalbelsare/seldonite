from io import BytesIO
import json
import logging
import math
import os
import re
from tempfile import TemporaryFile

import boto3
import botocore
from pyspark import SparkContext, SparkConf
from pyspark.sql import SQLContext, SparkSession
from pyspark.sql.types import StructType
from warcio.archiveiterator import ArchiveIterator
from warcio.recordloader import ArchiveLoadFailed
from bigdl.orca import init_orca_context, stop_orca_context


LOGGING_FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'


class CCSparkJob:
    """
    A simple Spark job definition to process Common Crawl data
    """

    name = 'CCSparkJob'

    warc_parse_http_header = True

    records_processed = None
    warc_input_processed = None
    warc_input_failed = None
    

    def __init__(self, num_input_partitions=64, local_temp_dir=None, 
                 log_level='INFO', spark_profiler=None, spark_master_url=None):

        # Number of input splits/partitions, number of parallel tasks to process WARC files/records
        self.num_input_partitions = num_input_partitions
        # Local temporary directory, used to buffer content from S3
        self.local_temp_dir = local_temp_dir
        # Logging level
        self.log_level = log_level
        # Enable PySpark profiler and log profiling metrics if job has finished, cf. spark.python.profile
        self.spark_profiler = spark_profiler
        # address of spark master node
        self.spark_master_url = spark_master_url

        self.use_bigdl = False

        logging.basicConfig(level=self.log_level, format=LOGGING_FORMAT)


    def get_output_options(self):
        return {x[0]: x[1] for x in map(lambda x: x.split('=', 1),
                                        self.output_option)}

    def init_logging(self, level=None):
        if level is None:
            level = self.log_level
        else:
            self.log_level = level
        logging.basicConfig(level=level, format=LOGGING_FORMAT)

    def init_accumulators(self, sc):
        self.records_processed = sc.accumulator(0)
        self.records_parsing_failed = sc.accumulator(0)
        self.warc_input_processed = sc.accumulator(0)
        self.warc_input_failed = sc.accumulator(0)

    def get_logger(self, spark_context=None):
        """Get logger from SparkContext or (if None) from logging module"""
        if spark_context is None:
            return logging.getLogger(self.name)
        return spark_context._jvm.org.apache.log4j.LogManager \
            .getLogger(self.name)

    def run(self, input_file_listing, url_only, archives=[]):
        '''
        params:
        input_file_listing: Path to file listing input paths
        '''

        self.url_only = url_only
        self.input_file_listing = input_file_listing
        self.archives = archives
        return self._run()

    def _run(self):
        conf = {}

        if self.spark_profiler:
            conf["spark.python.profile"] = "true"

        "spark.mongodb.input.uri", "mongodb://localhost:27017") \
    .config("spark.mongodb.output.uri", "mongodb://localhost:27017") \

        # add packages to allow pulling from AWS S3
        packages = [
            'com.amazonaws:aws-java-sdk-bundle:1.11.375',
            'org.apache.hadoop:hadoop-aws:3.2.0',
            'org.mongodb.spark:mongo-spark-connector_2.12:3.0.1'
        ]
        conf['spark.jars.packages'] = ','.join(packages)

        # anon creds for aws
        conf['spark.hadoop.fs.s3a.aws.credentials.provider'] = 'org.apache.hadoop.fs.s3a.AnonymousAWSCredentialsProvider'
        conf['spark.hadoop.fs.s3a.impl'] = 'org.apache.hadoop.fs.s3a.S3AFileSystem'

        conf['spark.app.name'] = self.name

        if self.spark_master_url:

            # set spark container image
            container_image = 'bigdl-k8s-spark-3.1.2-hadoop-3.2.0:latest'
            conf['spark.kubernetes.container.image'] = container_image

            # allow spark worker scaling
            dynamic_scaling = False
            if dynamic_scaling:
                conf['spark.dynamicAllocation.enabled'] = 'true'
                conf['spark.dynamicAllocation.shuffleTracking.enabled'] = 'true'
                conf["spark.dynamicAllocation.maxExecutors"] = "5"
            else:
                num_executors = 1
                conf['spark.executor.instances'] = str(num_executors)

            # specify pod size
            executor_cores = 32
            conf['spark.executor.cores'] = str(executor_cores)
            conf['spark.kubernetes.executor.request.cores'] = '28800m'
            executor_memory = '450g'
            conf['spark.executor.memory'] = executor_memory

            # add labels to pods
            conf['spark.kubernetes.executor.label.app'] = 'seldonite'

            # allow python deps to be used
            this_dir_path = os.path.dirname(os.path.abspath(__file__))
            conda_package_path = os.path.join(this_dir_path, 'seldonite_spark_env.tar.gz')
            os.environ['PYSPARK_PYTHON'] = '/opt/spark/work-dir/environment/bin/python'
            os.environ['BIGDL_CLASSPATH'] = '/opt/spark/work-dir/environment/lib/python3.7/site-packages/bigdl/share/dllib/lib/bigdl-dllib-spark_3.1.2-0.14.0-SNAPSHOT-jar-with-dependencies.jar:/opt/spark/work-dir/environment/lib/python3.7/site-packages/bigdl/share/orca/lib/bigdl-orca-spark_3.1.2-0.14.0-SNAPSHOT-jar-with-dependencies.jar'
            spark_archives = f'{conda_package_path}#environment'

            for archive_path in self.archives:
                dir_name = os.path.splitext(os.path.basename(archive_path))[0]
                spark_archives += f",{archive_path}#{dir_name}"

            conf['spark.archives'] = spark_archives

            if self.use_bigdl:
                sc = init_orca_context(cluster_mode='k8s-client',
                                       num_nodes=num_executors,
                                       cores=executor_cores,
                                       memory=executor_memory,
                                       master=self.spark_master_url, 
                                       container_image=container_image,
                                       conf=conf)
            else:
                spark_conf = SparkConf()
                spark_conf.setAll(conf.items())
                sc = SparkContext(
                    master=self.spark_master_url,
                    conf=spark_conf
                )
        else:
            os.environ['PYSPARK_PYTHON'] = 'python'
            if self.use_bigdl:
                sc = init_orca_context(cluster_mode='local',
                                       conf=conf)
            else:
                spark_conf = SparkConf()
                spark_conf.setAll(conf.items())
                sc = SparkContext(
                    conf=spark_conf
                )

        try:
            sqlc = SQLContext(sparkContext=sc)

            self.init_accumulators(sc)

            result = self.run_job(sc, sqlc)

            if self.spark_profiler:
                sc.show_profiles()

            return result

        finally:
            if self.use_bigdl:
                stop_orca_context()
                #pass
            else:
                sc.stop()

    def log_aggregator(self, sc, agg, descr):
        self.get_logger(sc).info(descr.format(agg.value))

    def log_aggregators(self, sc):
        self.log_aggregator(sc, self.warc_input_processed,
                            'WARC/WAT/WET input files processed = {}')
        self.log_aggregator(sc, self.warc_input_failed,
                            'WARC/WAT/WET input files failed = {}')
        self.log_aggregator(sc, self.records_processed,
                            'WARC/WAT/WET records processed = {}')
        self.log_aggregator(sc, self.records_parsing_failed,
                            'WARC/WAT/WET records parsing failed = {}')

    def run_job(self, sc, sqlc):
        input_data = sc.parallelize(self.input_file_listing,
                                 numSlices=self.num_input_partitions)

        rdd = input_data.mapPartitions(self.process_warcs)

        self.log_aggregators(sc)

        return self.process_dataset(sqlc, rdd)

    def process_warcs(self, iterator):
        s3pattern = re.compile('^s3://([^/]+)/(.+)')
        base_dir = os.path.abspath(os.path.dirname(__file__))

        # S3 client (not thread-safe, initialize outside parallelized loop)
        no_sign_request = botocore.client.Config(
            signature_version=botocore.UNSIGNED)
        s3client = boto3.client('s3', config=no_sign_request)

        for uri in iterator:
            self.warc_input_processed.add(1)
            if not uri.startswith('s3://'):
                raise ValueError('Cannot parse not S3 files with this implementation')

            self.get_logger().info('Reading from S3 {}'.format(uri))
            s3match = s3pattern.match(uri)
            if s3match is None:
                self.get_logger().error("Invalid S3 URI: " + uri)
                continue
            bucketname = s3match.group(1)
            path = s3match.group(2)
            warctemp = TemporaryFile(mode='w+b',
                                        dir=self.local_temp_dir)
            try:
                s3client.download_fileobj(bucketname, path, warctemp)
            except botocore.client.ClientError as exception:
                self.get_logger().error(
                    'Failed to download {}: {}'.format(uri, exception))
                self.warc_input_failed.add(1)
                warctemp.close()
                continue
            warctemp.seek(0)
            stream = warctemp

            no_parse = (not self.warc_parse_http_header)
            try:
                archive_iterator = ArchiveIterator(stream,
                                                   no_record_parse=no_parse, arc2warc=True)
                for res in self.iterate_records(uri, archive_iterator):
                    yield res
            except ArchiveLoadFailed as exception:
                self.warc_input_failed.add(1)
                self.get_logger().error(
                    'Invalid WARC: {} - {}'.format(uri, exception))
            finally:
                stream.close()

    def process_record(self, record):
        raise NotImplementedError('Processing record needs to be customized')

    def iterate_records(self, _warc_uri, archive_iterator):
        """Iterate over all WARC records. This method can be customized
           and allows to access also values from ArchiveIterator, namely
           WARC record offset and length."""
        records_successfully_processed = 0
        num_to_successfully_process = math.ceil(self.limit / self.num_input_partitions) if self.limit else None
        for record in archive_iterator:
            obj = self.process_record(record)
            if obj:
                yield obj
                records_successfully_processed += 1
            
            self.records_processed.add(1)

            # crude way of adding limiter
            if self.limit and records_successfully_processed >= num_to_successfully_process:
                break
            # WARC record offset and length should be read after the record
            # has been processed, otherwise the record content is consumed
            # while offset and length are determined:
            #  warc_record_offset = archive_iterator.get_record_offset()
            #  warc_record_length = archive_iterator.get_record_length()

    @staticmethod
    def is_wet_text_record(record):
        """Return true if WARC record is a WET text/plain record"""
        return (record.rec_type == 'conversion' and
                record.content_type == 'text/plain')

    @staticmethod
    def is_wat_json_record(record):
        """Return true if WARC record is a WAT record"""
        return (record.rec_type == 'metadata' and
                record.content_type == 'application/json')

    @staticmethod
    def is_html(record):
        """Return true if (detected) MIME type of a record is HTML"""
        html_types = ['text/html', 'application/xhtml+xml']
        if (('WARC-Identified-Payload-Type' in record.rec_headers) and
            (record.rec_headers['WARC-Identified-Payload-Type'] in
             html_types)):
            return True
        content_type = record.http_headers.get_header('content-type', None)
        if content_type:
            for html_type in html_types:
                if html_type in content_type:
                    return True
        return False


class CCIndexSparkJob(CCSparkJob):
    """
    Process the Common Crawl columnar URL index
    """

    name = "CCIndexSparkJob"

    # description of input and output shown in --help
    input_descr = "Path to Common Crawl index table"

    def __init__(self, table_path='s3a://commoncrawl/cc-index/table/cc-main/warc/', table_name='ccindex', query=None, **kwargs):
        super().__init__(**kwargs)
        # Name of the table data is loaded into
        self.table_name = table_name
        # SQL query to select rows (required)
        self.query = query
        # JSON schema of the ccindex table, implied from Parquet files if not provided.
        table_schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cc-index-schema-flat.json')
        
        with open(table_schema_path, 'r') as s:
            self.table_schema = StructType.fromJson(json.loads(s.read()))
        # path to default common crawl index
        self.table_path = table_path

    def load_table(self, sc, spark):
        parquet_reader = spark.read.format('parquet')
        parquet_reader = parquet_reader.schema(self.table_schema)
        df = parquet_reader.load(self.table_path)
        df.createOrReplaceTempView(self.table_name)
        self.get_logger(sc).info(
            "Schema of table {}:\n{}".format(self.table_name, df.schema))

    def execute_query(self, sc, spark, query):
        sqldf = spark.sql(query)
        self.get_logger(sc).info("Executing query: {}".format(query))
        sqldf.explain()
        return sqldf

    def load_dataframe(self, sc, session, partitions=-1):
        if self.query is not None:
            self.load_table(sc, session)
            sqldf = self.execute_query(sc, session, self.query)
        else:
            sqldf = session.read.format("csv").option("header", True) \
                .option("inferSchema", True).load(self.csv)
        sqldf.persist()

        num_rows = sqldf.count()
        self.get_logger(sc).info(
            "Number of records/rows matched by query: {}".format(num_rows))

        if partitions > 0:
            self.get_logger(sc).info(
                "Repartitioning data to {} partitions".format(partitions))
            sqldf = sqldf.repartition(partitions)

        return sqldf

    def run_job(self, sc, sqlc):
        session = SparkSession.builder.config(conf=sc.getConf()).getOrCreate()
        sqldf = self.load_dataframe(sc, session, self.num_input_partitions)

        self.log_aggregators(sc)

        return self.process_dataset(sqldf)

    def run(self):
        if self.query is None:
            raise ValueError('Please ensure query is set before running job.')
        return self._run()


class CCIndexWarcSparkJob(CCIndexSparkJob):
    """
    Process Common Crawl data (WARC records) found by the columnar URL index
    """

    name = "CCIndexWarcSparkJob"

    def __init__(self, query=None, csv=None, **kwargs):
        super().__init__(**kwargs)
        # SQL query to select rows. 
        # Note: the result is required to contain the columns `url', `warc_filename', `warc_record_offset' and `warc_record_length', make sure they're SELECTed. 
        # The column `content_charset' is optional and is utilized to read WARC record payloads with the right encoding.
        if query is not None:
            self.query = query
        # CSV file to load WARC records by filename, offset and length. 
        # The CSV file must have column headers 
        # and the input columns `url', `warc_filename', `warc_record_offset' and `warc_record_length' are mandatory, see also option query.
        self.csv = csv

    def fetch_process_warc_records(self, rows):
        no_sign_request = botocore.client.Config(
            signature_version=botocore.UNSIGNED)
        s3client = boto3.client('s3', config=no_sign_request)
        bucketname = "commoncrawl"
        no_parse = (not self.warc_parse_http_header)


        # TODO check for content truncated
        for row in rows:
            url = row['url']
            warc_path = row['warc_filename']
            offset = int(row['warc_record_offset'])
            length = int(row['warc_record_length'])
            content_charset = None
            if 'content_charset' in row:
                content_charset = row['content_charset']
            self.get_logger().debug("Fetching WARC record for {}".format(url))
            # TODO adapt if grouping warc records
            rangereq = 'bytes={}-{}'.format(offset, (offset+length-1))
            try:
                response = s3client.get_object(Bucket=bucketname,
                                               Key=warc_path,
                                               Range=rangereq)
            except botocore.client.ClientError as exception:
                self.get_logger().error(
                    'Failed to download: {} ({}, offset: {}, length: {}) - {}'
                    .format(url, warc_path, offset, length, exception))
                self.warc_input_failed.add(1)
                continue
            record_stream = BytesIO(response["Body"].read())
            try:
                for record in ArchiveIterator(record_stream,
                                              no_record_parse=no_parse):
                    # pass `content_charset` forward to subclass processing WARC records
                    record.rec_headers['WARC-Identified-Content-Charset'] = content_charset
                    article = self.process_record(record)
                    if article:
                        yield article

                    self.records_processed.add(1)

            except ArchiveLoadFailed as exception:
                self.warc_input_failed.add(1)
                self.get_logger().error(
                    'Invalid WARC record: {} ({}, offset: {}, length: {}) - {}'
                    .format(url, warc_path, offset, length, exception))

    def run_job(self, sc, sqlc):
        session = SparkSession.builder.config(conf=sc.getConf()).getOrCreate()
        sqldf = self.load_dataframe(sc, session, self.num_input_partitions)

        if self.url_only:
            columns = ['url']
            warc_recs = sqldf.select(*columns).rdd
            return warc_recs.flatMap(lambda x: x)
        else:
            columns = ['url', 'warc_filename', 'warc_record_offset', 'warc_record_length']
            if 'content_charset' in sqldf.columns:
                columns.append('content_charset')
            warc_recs = sqldf.select(*columns).rdd

        num_warcs = warc_recs.count()
        if num_warcs == 0:
            raise ValueError()

        rdd = warc_recs.mapPartitions(self.fetch_process_warc_records)

        self.log_aggregators(sc)

        return self.process_dataset(session, rdd)

    def run(self, url_only, archives=[]):
        self.url_only = url_only
        self.archives = archives
        return super().run()
