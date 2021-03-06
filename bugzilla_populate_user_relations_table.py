#!/usr/bin/env python
import time
import json
import multiprocessing
import sys
import yaml
import collections
from phabricator import Phabricator
from wmfphablib import Phab as phabmacros
from wmfphablib import phabdb
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

    pmig = phabdb.phdb(db=config.bzmigrate_db,
                       user=config.bzmigrate_user,
                       passwd=config.bzmigrate_passwd)

    issue = pmig.sql_x("SELECT id FROM bugzilla_meta WHERE id = %s", bugid)
    if not issue:
        log('issue %s does not exist for user population' % (bugid,))
        return True

    fpriority= pmig.sql_x("SELECT priority FROM bugzilla_meta WHERE id = %s", bugid)
    if fpriority[0] == ipriority['fetch_failed']:
        log('issue %s does not fetched successfully for user population (failed fetch)' % (bugid,))
        return True

    current = pmig.sql_x("SELECT priority, header, comments, created, modified FROM bugzilla_meta WHERE id = %s", bugid)
    if current:
        import_priority, buginfo, com, created, modified = current[0]
    else:
        log('%s not present for migration' % (bugid,))
        return True

    bzdata = open("data/bugzilla.yaml", 'r')
    bzdata_yaml = yaml.load(bzdata)
    mlists = bzdata_yaml['assigned_to_lists'].split(' ')
    vlog(mlists)
    header = json.loads(buginfo)
    vlog(str(header))
    relations = {}
    relations['author'] = header["creator"]
    relations['cc'] = header['cc']

    if header['assigned_to'] not in mlists:
        vlog("adding assignee %s to %s" % (header['assigned_to'], bugid))
        relations['owner'] = header['assigned_to']
    else:
        vlog("skipping %s assigned to %s" % (bugid, header['assigned_to']))
        relations['owner'] = ''


    for k, v in relations.iteritems():
        if relations[k]:
            relations[k] = filter(bool, v)

    def add_owner(owner):    
        ouser = pmig.sql_x("SELECT user FROM user_relations WHERE user = %s", (owner,))
        if ouser:
            jassigned = pmig.sql_x("SELECT assigned FROM user_relations WHERE user = %s", (owner,))
            jflat = tflatten(jassigned)
            if any(jflat):
                assigned = json.loads(jassigned[0][0])
            else:
                assigned = []
            if bugid not in assigned:
                log("Assigning %s to %s" % (str(bugid), owner))
                assigned.append(bugid)
            vlog("owner %s" % (str(assigned),))
            pmig.sql_x("UPDATE user_relations SET assigned=%s, modified=%s WHERE user = %s", (json.dumps(assigned),
                                                                                              now(),
                                                                                              owner))
        else:
            vlog('inserting new record')
            assigned = json.dumps([bugid])
            insert_values =  (owner,
                              assigned,
                              now(),
                              now())

            pmig.sql_x("INSERT INTO user_relations (user, assigned, created, modified) VALUES (%s, %s, %s, %s)",
                       insert_values)


    def add_author(author):
        euser = pmig.sql_x("SELECT user FROM user_relations WHERE user = %s", (relations['author'],))
        if euser:
            jauthored = pmig.sql_x("SELECT author FROM user_relations WHERE user = %s", (relations['author'],))
            jflat = tflatten(jauthored)
            if any(jflat):
                authored = json.loads(jauthored[0][0])
            else:
               authored = []
            if bugid not in authored:
                authored.append(bugid)
            vlog("author %s" % (str(authored),))
            pmig.sql_x("UPDATE user_relations SET author=%s, modified=%s WHERE user = %s", (json.dumps(authored),
                                                                                        now(),
                                                                                        relations['author']))
        else:
            vlog('inserting new record')
            authored = json.dumps([bugid])
            insert_values =  (relations['author'],
                              authored,
                              now(),
                              now())
            pmig.sql_x("INSERT INTO user_relations (user, author, created, modified) VALUES (%s, %s, %s, %s)",
                       insert_values)


    def add_cc(ccuser):
        eccuser = pmig.sql_x("SELECT user FROM user_relations WHERE user = %s", (ccuser,))
        if eccuser:
            jcc = pmig.sql_x("SELECT cc FROM user_relations WHERE user = %s", (ccuser,))
            jflat = tflatten(jcc)
            if any(jflat):
               cc = json.loads(jcc[0][0])
            else:
               cc = []
            if bugid not in cc:
                cc.append(bugid)
            vlog("cc %s" % (str(cc),))
            pmig.sql_x("UPDATE user_relations SET cc=%s, modified=%s WHERE user = %s", (json.dumps(cc),
                                                                                        now(),
                                                                                        ccuser))
        else:
            vlog('inserting new record')
            cc = json.dumps([bugid])
            insert_values =  (ccuser,
                              cc,
                              now(),
                              now())
            pmig.sql_x("INSERT INTO user_relations (user, cc, created, modified) VALUES (%s, %s, %s, %s)",
                   insert_values)

    if relations['author']:
        add_author(relations['author'])

    if relations['owner']:
        add_owner(relations['owner'])

    if relations['cc']:
        for u in filter(bool, relations['cc']):
            add_cc(u)

    pmig.close()
    return True

def run_populate(bugid, tries=1):
    if tries == 0:
        elog('user relations failed to populate for %s' % (bugid,))
        return False
    try:
        return populate(bugid)
    except Exception as e:
        import traceback
        tries -= 1
        time.sleep(5)
        traceback.print_exc(file=sys.stdout)
        elog('user relations failed to populate %s (%s)' % (bugid, e))
        return run_populate(bugid, tries=tries)

def main():
    bugs = return_bug_list()
    result = []
    for b in bugs:
        result.append(run_populate(b))
    complete = len(filter(bool, result))
    failed = len(result) - complete
    print '%s completed %s, failed %s' % (sys.argv[0], complete, failed)

if __name__ == '__main__':
    main()
