from taobao.settings import REDIS_HOST, CAPTCHA_CHECK_INTERVAL, CRAWLER_HEARTBEAT_TIMEOUT
from taobao.utils import image_from_string
import redis
import time
import ast


class Monitor(object):

    def __init__(self):
        self.r = redis.StrictRedis(host=REDIS_HOST, port=6379, db=0)
        self.r_to_crawler = None

    def add_account(self, username, password):
        self.r.sadd('account_set', (username, password).__str__())

    def stats(self):
        crawler_list = self.get_crawler_list()
        # TODO: add account stats
        pipe = self.r.pipeline()
        print "---------------------ACCOUNT STATS---------------------"
        account_set = self.r.sscan(name='account_set', count=10000)
        account_set = account_set[1]
        for crawler in crawler_list:
            pipe.get('crawler_account:%s' % crawler[0])
        crawling_account_list = pipe.execute()
        for avail_account in account_set:
            (username, password) = ast.literal_eval(avail_account)
            print "%s (available)" % username
        for avail_account in crawling_account_list:
            (username, password) = ast.literal_eval(avail_account)
            print "%s (used)" % username
        print "---------------------CRAWLER STATS---------------------"
        for crawler in crawler_list:
            print "%s on %s:%s" % (crawler[0], crawler[1], crawler[2])
        print "Total: %d" % len(crawler_list)

        pipe.execute()

    def get_crawler_list(self):
        lst = self.r.sscan(name='crawler_id_set', count=10000)[1]
        pipe = self.r.pipeline()
        for i in range(len(lst)):
            pipe.get('crawler:ip:%s' % lst[i])

        ip_lst = pipe.execute()
        for i in range(len(lst)):
            pipe.get('crawler:port:%s' % lst[i])
        port_lst = pipe.execute()
        for i in range(len(lst)):
            lst[i] = (lst[i], ip_lst[i], port_lst[i])
        return lst

    def clear_expired_crawlers(self):
        crawler_list = self.get_crawler_list()
        for crawler in crawler_list:
            print "checking crawler %s on %s" % (crawler[0], crawler[1])
            self.r_to_crawler = redis.StrictRedis(
                host=crawler[1], port=crawler[2], db=0)
            try:
                heartbeat = float(
                    self.r_to_crawler.get('crawler:heartbeat:%s' % crawler[0]))
                timestamp = self.r_to_crawler.time()[0]
            except:
                print "cannot get heartbeat or timestamp for crawler %s on %s:%s" % (crawler[0], crawler[1], str(crawler[2]))
                if self.r.srem('crawler_id_set', crawler[0]):
                    account = self.r.get('crawler_account:%s' % crawler[0])
                    if account:
                        self.r.sadd('account_set', account)
                    print "removed crawler %s from crawler id set" % crawler[0]
                continue
            if not heartbeat or not timestamp:
                print "cannot get heartbeat or timestamp for crawler %s on %s:%s" % (crawler[0], crawler[1], str(crawler[2]))
                if self.r.srem('crawler_id_set', crawler[0]):
                    account = self.r.get('crawler_account:%s' % crawler[0])
                    if account:
                        self.r.sadd('account_set', account)
                    print "removed crawler %s from crawler id set" % crawler[0]
                continue
            if timestamp - heartbeat > CRAWLER_HEARTBEAT_TIMEOUT:
                # heartbeat timeout
                print "heartbeat of crawler %s on %s:%s timeout" % (crawler[0], crawler[1], str(crawler[2]))
                if self.r.srem('crawler_id_set', crawler[0]):
                    account = self.r.get('crawler_account:%s' % crawler[0])
                    if account:
                        self.r.sadd('account_set', account)
                    print "removed crawler %s from crawler id set" % crawler[0]

    # move expired user from proc set to new queue
    # NOTICE: This method checks upto 10000 proc ids per execution
    def recrawl_expired_users(self):
        self.clear_expired_crawlers()
        print "cleared expired crawlers"
        pipe = self.r.pipeline()
        pipe.sscan(name='proc_id_set', count=10000)
        pipe.sscan(name='crawler_id_set', count=10000)
        (proc_id_set, crawler_id_set) = pipe.execute()
        proc_id_set = proc_id_set[1]
        crawler_id_set = crawler_id_set[1]
        print "fetched proc_id_set(%d) and crawler_id_set(%d)" % (len(proc_id_set), len(crawler_id_set))

        for i in range(len(proc_id_set)):
            pipe.get('crawler_id:%s' % proc_id_set[i])
        proc_crawler_id_list = pipe.execute()
        print "fetched corresponding crawler id list (%d)" % len(proc_crawler_id_list)

        remove_user_id_list = []
        for i in range(len(proc_crawler_id_list)):
            if not proc_crawler_id_list[i] or proc_crawler_id_list[i] not in crawler_id_set:
                pipe.srem('proc_id_set', proc_id_set[i])
                remove_user_id_list.append(proc_id_set[i])
        remove_status = pipe.execute()

        print "remove expired users(%d)" % len(remove_user_id_list)

        for i in range(len(remove_status)):
            if remove_status[i] == 1:
                pipe.rpush('new_id_queue', remove_user_id_list[i])
        pipe.execute()

        print "put expired users into new_id_queue"

    def solve_captcha(self, crawler_id, crawler_ip, crawler_port):
        self.r_to_crawler = redis.StrictRedis(
            host=crawler_ip, port=crawler_port, db=0)
        # TODO: consider lock here(probably multiple monitor)
        if not self.r_to_crawler.get('crawler:status:%s' % crawler_id) == 'captcha_input':
            return
        print 'found captcha for crawler %s on %s' % (crawler_id, crawler_ip)
        self.r_to_crawler.set(
            'crawler:status:%s' % crawler_id, 'captcha_snapshot')
        print 'captcha status for crawler %s on %s set to "captcha_snapshot"' % (crawler_id, crawler_ip)
        # wait for client to take snapshot
        while True:
            captcha = self.r_to_crawler.get('captcha:%s' % crawler_id)
            if captcha:
                print 'got captcha for crawler %s on %s' % (crawler_id, crawler_ip)
                im = image_from_string(captcha)
                im.show()
                self.r_to_crawler.set(
                    'captcha_input:%s' % crawler_id, raw_input("captcha:"))
                print 'set captcha for crawler %s on %s' % (crawler_id, crawler_ip)
                while True:
                    # check if client made it through
                    if self.r_to_crawler.get('crawler:status:%s' % crawler_id) == 'good':
                        print 'solved captcha for crawler %s on %s' % (crawler_id, crawler_ip)
                        return
                    elif self.r_to_crawler.get('crawler:status:%s' % crawler_id) == 'captcha_input':
                        print 'failed to solve captcha for crawler %s on %s' % (crawler_id, crawler_ip)
                        return
                    time.sleep(CAPTCHA_CHECK_INTERVAL)
                    # TODO: timeout check
                break
            time.sleep(CAPTCHA_CHECK_INTERVAL)

    def solve_captchas(self):
        crawler_list = self.get_crawler_list()
        for crawler in crawler_list:
            crawler_id = crawler[0]
            crawler_ip = crawler[1]
            crawler_port = crawler[2]
            if crawler_id and crawler_ip:
                self.solve_captcha(crawler_id, crawler_ip, crawler_port)

    def maintain_consistency(self):
        id_set = self.r.sscan(name='id_set', count=5000000)
        new_list = self.r.lrange('new_id_queue', 0, 5000000)
        finish_list = self.r.lrange('finish_id_queue', 0, 5000000)
        error_list = self.r.lrange('error_id_queue', 0, 5000000)
        # remove redundancy
        new_count = {}
        for idd in new_list:
            if idd in new_count:
                new_count[idd] += 1
            else:
                new_count[idd] = 1

        finish_count = {}
        for idd in finish_list:
            if idd in finish_count:
                finish_count[idd] += 1
            else:
                finish_count[idd] = 1

        error_count = {}
        for idd in error_list:
            if idd in error_count:
                error_count[idd] += 1
            else:
                error_count[idd] = 1
        # remove duplicate
        # queue missing ids
