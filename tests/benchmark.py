from time import time
from tests.models import *
from mongosql.handlers import MongoJoin


# Timer class
class timer(object):
    def __init__(self):
        self.total = 0.0
        self._current = time()

    def stop(self, ):
        self.total += time() - self._current
        return self

# Take the scariest query with joins, and execute it many times over
N_QUERIES = 1000


for big_db in (False, True):
    # Init the DB
    print()
    if big_db:
        print('Test: with a large result set')
        engine, Session = get_big_db_for_benchmarks(n_users=100, n_articles_per_user=5, n_comments_per_article=3)
    else:
        print('Test: with a small result set')
        engine, Session = get_working_db_for_tests()

    # Session
    print('Starting benchmarks....')
    ssn = Session()

    # The benchmark itself
    for selectinquery_enabled in (True, False):
        # Enable/Disable selectinquery()
        MongoJoin.ENABLED_EXPERIMENTAL_SELECTINQUERY = selectinquery_enabled

        qs = [None for i in range(N_QUERIES)]

        # Benchmark
        total_timer = timer()
        mongosql_timer = timer()
        for i in range(len(qs)):
            # using query from test_join__one_to_many()
            mq = User.mongoquery(ssn).query(
                project=['name'],
                filter={'age': {'$ne': 100}},
                join={'articles': dict(project=['title'],
                                        filter={'theme': {'$ne': 'sci-fi'}},
                                        join={'comments': dict(project=['aid'],
                                                                filter={'text': {'$exists': True}})})}
            )
            qs[i] = mq.end()
        mongosql_timer.stop()

        sqlalchemy_timer = timer()
        for q in qs:
            list(list(
                list(a.comments) for a in u.articles
            ) for u in q.all())  # load all, force sqlalchemy to process every row
        sqlalchemy_timer.stop()
        total_timer.stop()

        ms_per_query = total_timer.total / N_QUERIES * 1000

        print(f'{"with" if selectinquery_enabled else "without"} selectinquery: {total_timer.total:0.2f}s '
              f'(mongosql: {mongosql_timer.total:0.2f}s, sqlalchemy: {sqlalchemy_timer.total:0.2f}s), {ms_per_query:.02f}ms/query')

# Current run time with 3000 queries, Python 3.7

# Test: with a small result set (~10 rows * few related)
# with selectinquery: 4.64s (mongosql: 0.67s, sqlalchemy: 3.98s), 4.64ms/query
# without selectinquery: 7.74s (mongosql: 4.78s, sqlalchemy: 2.95s), 7.74ms/query
#
# Test: with a large result set (100 rows * 5 related * 3 related)
# with selectinquery: 108.24s (mongosql: 1.45s, sqlalchemy: 106.79s), 108.24ms/query
# without selectinquery: 102.00s (mongosql: 5.10s, sqlalchemy: 96.90s), 102.00ms/query
