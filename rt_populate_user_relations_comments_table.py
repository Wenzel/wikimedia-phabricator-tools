#!/usr/bin/env python
import time
import json
import multiprocessing
import sys
import collections
from phabricator import Phabricator
from wmfphablib import Phab as phabmacros
from wmfphablib import phabdb
from wmfphablib import rtlib
from wmfphablib import log
from wmfphablib import vlog
from wmfphablib import errorlog as elog
from wmfphablib import config
from wmfphablib import epoch_to_datetime
from wmfphablib import ipriority
from wmfphablib import now
from wmfphablib import tflatten
from wmfphablib import return_bug_list


def populate(bugid):

    def add_comment_ref(owner):    
        """ adds an issue reference to a user or later updating their comments
        """
        ouser = pmig.sql_x("SELECT user FROM user_relations_comments WHERE user = %s", (owner,))
        if ouser:
            jcommed = pmig.sql_x("SELECT issues FROM user_relations_comments WHERE user = %s", (owner,))
            if jcommed and any(tflatten(jcommed)):
                issues = json.loads(jcommed[0][0])
            else:
                issues = []

            if bugid not in issues:
                log("Comment reference %s to %s" % (str(bugid), owner))
                issues.append(bugid)
            pmig.sql_x("UPDATE user_relations_comments SET issues=%s, modified=%s WHERE user = %s", (json.dumps(issues),
                                                                                                     now(),
                                                                                                     owner))
        else:
            issues = json.dumps([bugid])
            insert_values =  (owner,
                              issues,
                              now(),
                              now())

            pmig.sql_x("INSERT INTO user_relations_comments (user, issues, created, modified) VALUES (%s, %s, %s, %s)",
                       insert_values)


    pmig = phabdb.phdb(db=config.rtmigrate_db,
                       user=config.rtmigrate_user,
                       passwd=config.rtmigrate_passwd)

    issue = pmig.sql_x("SELECT id FROM rt_meta WHERE id = %s", bugid)
    if not issue:
        log('issue %s does not exist for user population' % (bugid,))
        return True

    fpriority= pmig.sql_x("SELECT priority FROM rt_meta WHERE id = %s", bugid)
    if fpriority[0] == ipriority['fetch_failed']:
        log('issue %s does not fetched successfully for user population (failed fetch)' % (bugid,))
        return True

    current = pmig.sql_x("SELECT comments, xcomments, modified FROM rt_meta WHERE id = %s", bugid)
    if current:
        comments, xcomments, modified = current[0]
    else:
        log('%s not present for migration' % (bugid,))
        return 'missing'

    com = json.loads(comments)
    xcom = json.loads(xcomments)
    # rtlib.user_lookup(header["Creator"])
    commenters = [rtlib.user_lookup(c['author']) for c in xcom.values() if c['count'] > 0]
    commenters = set(filter(bool, commenters))
    print commenters
    log("commenters for issue %s: %s" % (bugid, str(commenters)))
    for c in commenters:
        print c
        add_comment_ref(c)
    pmig.close()
    return True

def run_populate(bugid, tries=1):
    if tries == 0:
        elog('failed to populate for %s' % (bugid,))
        return False
    try:
        return populate(bugid)
    except Exception as e:
        import traceback
        tries -= 1
        time.sleep(5)
        traceback.print_exc(file=sys.stdout)
        elog('failed to populate %s' % (bugid,))
        return run_populate(bugid, tries=tries)


def main():
    bugs = return_bug_list()
    result = []
    for b in bugs:
        result.append(run_populate(b))

    missing = len([i for i in result if i == 'missing'])
    complete = len(filter(bool, [i for i in result if i not in ['missing']]))
    failed = (len(result) - missing) - complete
    print '-----------------------------\n \
          %s Total %s (missing %s)\n \
          completed %s, failed %s' % (sys.argv[0],
                                                          len(bugs),
                                                          missing,
                                                          complete,
                                                          failed)
if __name__ == '__main__':
    main()
