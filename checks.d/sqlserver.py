'''
Check the performance counters from SQL Server
'''
# stdlib
import traceback

# project
from checks import AgentCheck

# 3rd party
import adodbapi

ALL_INSTANCES = 'ALL'
VALID_METRIC_TYPES = ('gauge', 'rate', 'histogram')

PERF_LARGE_RAW_BASE = 1073939712
PERF_RAW_LARGE_FRACTION = 537003264
PERF_AVERAGE_BULK = 1073874176
PERF_COUNTER_BULK_COUNT = 272696576
PERF_COUNTER_LARGE_RAWCOUNT = 65792

class SQLServer(AgentCheck):

    SOURCE_TYPE_NAME = 'sql server'

    METRICS = [
        ('sqlserver.buffer.cache_hit_ratio', 'Buffer cache hit ratio', ''), # RAW_LARGE_FRACTION
        ('sqlserver.buffer.page_life_expectancy', 'Page life expectancy', ''), # LARGE_RAWCOUNT
        ('sqlserver.stats.batch_requests', 'Batch Requests/sec', ''), # BULK_COUNT
        ('sqlserver.stats.sql_compilations', 'SQL Compilations/sec', ''), # BULK_COUNT
        ('sqlserver.stats.sql_recompilations', 'SQL Re-Compilations/sec', ''), # BULK_COUNT
        ('sqlserver.stats.connections', 'User connections', ''), # LARGE_RAWCOUNT
        ('sqlserver.stats.lock_waits', 'Lock Waits/sec', '_Total'), # BULK_COUNT
        ('sqlserver.access.page_splits', 'Page Splits/sec', ''), # BULK_COUNT
        ('sqlserver.stats.procs_blocked', 'Processes Blocked', ''), # LARGE_RAWCOUNT
        ('sqlserver.buffer.checkpoint_pages', 'Checkpoint pages/sec', '') #BULK_COUNT
    ]

    def __init__(self, name, init_config, agentConfig, instances = None):
        AgentCheck.__init__(self, name, init_config, agentConfig, instances)

        # Cache connections
        self.connections = {}

        self.instances_metrics = {}
        for instance in instances:

            metrics_to_collect = []
            for metric in METRICS:
                name, counter_name, instance_name = metric
                try:
                    sql_type = self.get_sql_type(instance, sql_name)
                    metrics_to_collect.append(self.typed_metric(name,
                                                                counter_name,
                                                                None,
                                                                sql_type,
                                                                instance_name,
                                                                None))
                except Exception:
                    self.log.warning("Can't load the metric %s, ignoring", name)
                    continue

            # Load any custom metrics from conf.d/sqlserver.yaml
            for row in init_config.get('custom_metrics', []):
                type = row.get('type')
                if type is not None and type not in VALID_METRIC_TYPES:
                    self.log.error('%s has an invalid metric type: %s' \
                                    % (row['name'], type))
                sql_type = None
                try:
                    if type is None:
                        sql_type = self.get_sql_type(instance, row['counter_name'])
                except Exception:
                    self.log.warning("Can't load the metric %s, ignoring", name)
                    continue


               metrics_to_collect.append(self.typed_metric(row['name'],
                                                           row['counter_name'],
                                                           type,
                                                           sql_tpye,
                                                           row.get('instance_name', ''),
                                                           row.get('tag_by', None)))


            instance_key = self._conn_key(instance)
            self.instances_metrics[instance_key] = metrics_to_collect
    def typed_metrics(self, dd_name, sql_name, type, sql_type, instance_name, tag_by):
        if type is not None or sql_type in [PERF_COUNTER_BULK_COUNT,
                                            PERF_COUNTER_LARGE_RAWCOUNT]:
            if type is None:
                type = "gauge" if sql_type==PERF_COUNTER_BULK_COUNT else "rate"
            func = getattr(self, type)
            return SqlSimpleMetric(dd_name, sql_name, func, instance_name, tag_by)


    def _conn_key(self, instance):
        ''' Return a key to use for the connection cache
        '''
        host = instance.get('host', '127.0.0.1;1433')
        username = instance.get('username')
        password = instance.get('password')
        database = instance.get('database', 'master')
        return '%s:%s:%s:%s' % (host, username, password, database)

    def _conn_string(self, host, username, password, database):
        ''' Return a connection string to use with adodbapi
        '''
        conn_str = 'Provider=SQLOLEDB;Data Source=%s;Initial Catalog=%s;' \
                        % (host, database)
        if username:
            conn_str += 'User ID=%s;' % (username)
        if password:
            conn_str += 'Password=%s;' % (password)
        if not username and not password:
            conn_str += 'Integrated Security=SSPI;'
        return conn_str

    def get_cursor(self, instance):
        conn_key = self._conn_key(instance)

        if conn_key not in self.connections:
            try:
                conn_str = self._conn_string(host, username, password, database)
                conn = adodbapi.connect(conn_str)
                self.connections[conn_key] = conn
            except Exception, e:
                cx = "%s - %s" % (host, database)
                raise Exception("Unable to connect to SQL Server for instance %s.\n %s" \
                    % (cx, traceback.format_exc()))

        conn = self.connections[conn_key]
        cursor = conn.cursor()

    def check(self, instance):
        cursor = self.get_cursor(instance)
        self._fetch_metrics(cursor, instance)

    def get_sql_type(self, instance, counter_name):
        '''
        Return the type of the performance counter so that we can report it to
        Datadog correctly
        '''
        cursor = self.get_cursor(instance)
        cursor.execute("""
            select cntr_type
            from sys.dm_os_performance_counters
            where counter_name = ?
            """, (counter_name))
        (value,) = cursor.fetchone()
        if value == PERF_RAW_LARGE_BASE:
            self.log.warning("Metric %s is of type Base and shouldn't be reported this way")
        return value

    def _fetch_metrics(self, cursor, instance):
        ''' Fetch the metrics from the sys.dm_os_performance_counters table
        '''
        custom_tags = instance.get('tags', [])
        instance_key = self._conn_key(instance)
        metrics_to_collect = self.instances_metrics[instance_key]
        cursor = self.get_cursor(instance)
        for metric in metrics_to_collect:
            metric.fetch_metrics(cursor, tags)

class SqlServerMetric(Object):
 
    def __init__(self, datadog_name, sql_name, report_function,
                 instance, tag_by):
        self.datadog_name = datadog_name
        self.sql_name = sql_name
        self.report_function = report_function
        self.instance = instance
        self.tag_by = tag_by

    def fetch_metric(self, cursor, tags):
        raise NotImplementedError

class SqlSimpleMetric(Object):

    def fetch_metric(self, cursor, tags):
        query_base = '''
                    select instance_name, cntr_value
                    from sys.dm_os_performance_counters
                    where counter_name = ?
                    '''
        if self.instance == ALL_INSTANCES:
            query = query_base + "and instance_name!= '_Total'"
            query_content = (self.sql_name,)
        else:
            query = query_base + "and instance_name=?"
            query_content = (self.sql_name,self.instance)

        cursor.execute(query,data)
        rows = cursor.fetchall()
        for instance_name, cntr_value in rows:
            metric_tags = tags
            if self.instance_= ALL_INSTANCES:
                metric_tags = tags + ['%s:%s' % (tag_by, instance_name.strip())]
            self.report_function(self.datadog_name, cntr_value,
                                 tags=metric_tags)
