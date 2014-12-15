#!/usr/bin/env python
#from __future__ import unicode_literals
import time
import json
import os
import re
import sys
import getpass
sys.path.append('/home/rush/python-rtkit/')
from phabricator import Phabricator
from wmfphablib import Phab as phabmacros
from wmfphablib import errorlog as elog
from wmfphablib import return_bug_list
from wmfphablib import phdb
from wmfphablib import phabdb
from wmfphablib import mailinglist_phid
from wmfphablib import set_project_icon
from wmfphablib import log
from wmfphablib import util
from wmfphablib import rtlib
from wmfphablib import vlog
from wmfphablib import config
from wmfphablib import rtlib
from wmfphablib import datetime_to_epoch
from wmfphablib import epoch_to_datetime
from wmfphablib import now
from rtkit import resource
from rtkit import authenticators
from rtkit import errors
from wmfphablib import ipriority


def create(rtid):

    phab = Phabricator(config.phab_user,
                       config.phab_cert,
                       config.phab_host)

    phabm = phabmacros('', '', '')
    phabm.con = phab

    pmig = phdb(db=config.rtmigrate_db)

    response = resource.RTResource(config.rt_url,
                                   config.rt_login,
                                   config.rt_passwd,
                                   authenticators.CookieAuthenticator)

    current = pmig.sql_x("SELECT priority, header, \
                          comments, created, modified \
                          FROM rt_meta WHERE id = %s",
                          (rtid,))
    if current:
        import_priority, rtinfo, com, created, modified = current[0]
    else:
        elog('%s not present for migration' % (rtid,))
        return False

    if not rtinfo:
        log("ignoring invalid data for issue %s" % (rtid,))
        return False
        
    def get_ref(id):
        refexists = phabdb.reference_ticket('%s%s' % (rtlib.prepend, id))
        if refexists:
            return refexists

    if get_ref(rtid):
        log('reference ticket %s already exists' % (rtid,))
        return True

    viewpolicy = phabdb.get_project_phid('WMF-NDA')
    if not viewpolicy:
        elog("View policy group not present: %s" % (viewpolicy,))
        return False

    def remove_sig(content):
        return re.split('--\s?\n', content)[0]

    # Example:
    # id: ticket/8175/attachments\n
    # Attachments: 141490: (Unnamed) (multipart/mixed / 0b),
    #              141491: (Unnamed) (text/html / 23b),
    #              141492: 0jp9B09.jpg (image/jpeg / 117.4k),
    attachments = response.get(path="ticket/%s/attachments/" % (rtid,))
    if not attachments:
        raise Exception("no attachment response: %s" % (rtid))

    history = response.get(path="ticket/%s/history?format=l" % (rtid,))


    rtinfo = json.loads(rtinfo)
    comments = json.loads(com)
    vlog(rtid)
    vlog(rtinfo)

    comment_dict = {}
    for i, c in enumerate(comments):
        cwork = {}
        comment_dict[i] = cwork
        if not 'Attachments:' in c:
            pass
        attachsplit = c.split('Attachments:')
        if len(attachsplit) > 1:
            body, attached = attachsplit[0], attachsplit[1]
        else:
            body, attached = c, '0'
        comment_dict[i]['text_body'] = body
        comment_dict[i]['attached'] = attached

    # Example:
    # Ticket: 8175\nTimeTaken: 0\n
    # Type: 
    # Create\nField:
    # Data: \nDescription: Ticket created by cpettet\n\n
    # Content: test ticket description\n\n\n
    # Creator: cpettet\nCreated: 2014-08-21 21:21:38\n\n'}
    params = {'id': 'id:(.*)',
              'ticket': 'Ticket:(.*)',
              'timetaken': 'TimeTaken:(.*)',
              'content': 'Content:(.*)',
              'creator': 'Creator:(.*)',
              'description': 'Description:(.*)',
              'created': 'Created:(.*)',
              'ovalue': 'OldValue:(.*)',
              'nvalue': 'NewValue:(.*)'}

    for k, v in comment_dict.iteritems():
        text_body = v['text_body']
        comment_dict[k]['body'] = {}
        for paramkey, regex in params.iteritems():
            value = re.search(regex, text_body)
            if value:
                comment_dict[k]['body'][paramkey] = value.group(1).strip()
            else:
                comment_dict[k]['body'][paramkey] = None

        if 'Content' in text_body:
            content = text_body.split('Content:')[1]
            content = content.split('Creator:')
            comment_dict[k]['body']['content'] = content

        creator = comment_dict[k]['body']['creator']
        if creator and '@' in creator:
            comment_dict[k]['body']['creator'] = rtlib.sanitize_email(creator)

        #15475: untitled (18.7k)
        comment_attachments= re.findall('(\d+):\s', v['attached'])
        comment_dict[k]['body']['attached'] = comment_attachments

    # due to the nature of the RT api sometimes whitespacing becomes
    # a noise comment
    if not any(comment_dict[comment_dict.keys()[0]]['body'].values()):
        vlog('dropping %s comment' % (str(comment_dict[comment_dict.keys()[0]],)))
        del comment_dict[0]

    #attachments into a dict
    def attach_to_kv(attachments_output):
        attached = re.split('Attachments:', attachments_output, 1)[1]
        ainfo = {}
        for at in attached.strip().splitlines():
            if not at:
                continue
            k, v = re.split(':', at, 1)
            ainfo[k.strip()] = v.strip()
        return ainfo

    ainfo = attach_to_kv(attachments)
    #lots of junk attachments from emailing comments and ticket creation
    ainfo_f = {}
    for k, v in ainfo.iteritems():
        if '(Unnamed)' not in v:
            ainfo_f[k] = v

    #taking attachment text and convert to tuple (name, content type, size)
    ainfo_ext = {}
    comments = re.split("\d+\/\d+\s+\(id\/.\d+\/total\)", history)
    attachregex = '(.*)\.(\S{3,4})\s\((.*)\s\/\s(.*)\)'
    for k, v in ainfo_f.iteritems():
        # Handle general attachment case:
        # NO: 686318802.html (application/octet-stream / 19.5k),
        # YES: Summary_686318802.pdf (application/unknown / 215.3k),
        print attachregex
        print v
        extract = re.search(attachregex, v)
        print extract
        # due to goofy email handling of signature/x-header/meta info
        # it seems they sometimes
        # become malformed attachments.  Such as when a response into
        # rt was directed to a mailinglist
        # Example:
        #     ->Attached Message Part (text/plain / 158b)
        #
        #    Private-l mailing list
        #    Private-l@lists.wikimedia.org
        #    https://lists.wikimedia.org/mailman/listinfo/private-l
        if extract:
            print "YES"
            vlog(extract.groups())
            ainfo_ext[k] = extract.groups()
        elif not extract and v.startswith('Attached Message Part'):
            continue
        else:
            elog("no attachment CORRUPT or FAILED extraction: %s %s (%s)" % (k, v, rtid))
    print ainfo_ext

    attachment_types = ['pdf',
                        'jpeg',
                        'tgz',
                        'jpg',
                        'png',
                        'xls',
                        'xlsx',
                        'gif',
                        'html',
                        'htm',
                        'txt',
                        'log',
                        'zip',
                        'rtf',
                        'vcf',
                        'eml']

    #Uploading attachment
    dl = []
    #('Quote Summary_686318802', 'pdf', 'application/unknown', '215.3k')
    uploaded = {}
    for k, v in ainfo_ext.iteritems():
        file_extension = v[1].lower()
        # vendors have this weird habit of capitalizing extension names
        # make sure we can handle the extension type otherwise
        if file_extension not in attachment_types:
            log("%s %s %s" % (rtid, v, file_extension))
            raise Exception('unknown extension: %s (%s)' % (v, rtid))
        full = "ticket/%s/attachments/%s/content" % (rtid, k)

        vcontent = response.get(path=full,
                                headers={'Content-Type': v[2], 'Content-Length': v[3] })
        try:
            #PDF's don't react well to stripping header -- fine without it
            if file_extension.strip() == 'pdf':
                sanscontent = str(''.join(vcontent.readlines()))
            else:
                log("%s.%s" % (v[0], file_extension))
                vcontent = str(vcontent.readlines())
                sanscontent = ''.join(vcontent[2:])
            upload = phabm.upload_file("%s.%s" % (v[0], file_extension),
                                       sanscontent,
                                      viewpolicy)
            uploaded[k] = upload

        except Exception as e:
            print e
            #elog("Attachment CORRUPT in source: %s" % (v[0] + file_extension,))

    return
    if rtinfo['Queue'] not in rtlib.enabled:
        log("%s not in an enabled queue" % (rtid,))
        return True

    ptags = []
    if rtinfo['Queue'] == 'ops-requests':
        ptags.append('operations')

    pname = rtlib.project_translate(rtinfo['Queue'])
    ptags.append(pname)

    phids = []
    for p in ptags:
        phids.append(phabm.ensure_project(p))

    rtinfo['xpriority'] = rtlib.priority_convert(rtinfo['Priority'])
    rtinfo['xstatus'] = rtlib.status_convert(rtinfo['Status'])

    import collections
    # {'ovalue': u'open',
    # 'description': u"Status changed from 'open' to 'resolved' by robh",
    # 'nvalue': None, 'creator': u'robh', 'attached': [],
    # 'timetaken': u'0', 'created': u'2011-07-01 02:47:24', 
    # 'content': [u' This transaction appears to have no content\n', u'
    #              robh\nCreated: 2011-07-01 02:47:24\n'],
    # 'ticket': u'1000', 'id': u'23192'}
    ordered_comments = collections.OrderedDict(sorted(comment_dict.items()))
    upfiles = uploaded.keys()

    # much like bugzilla comment 0 is the task description
    header = comment_dict[comment_dict.keys()[0]]
    del comment_dict[comment_dict.keys()[0]]
    dtext = '\n'.join([l.strip() for l in header['body']['content'][0].splitlines()])
    dtext = rtlib.shadow_emails(dtext)
    full_description = "**Author:** `%s`\n\n**Description:**\n%s\n" % (rtinfo['Creator'].strip(),
                                                                       dtext)


    hafound = header['body']['attached']
    header_attachments = []
    for at in hafound:
        if at in upfiles:
            header_attachments.append('{F%s}' % uploaded[at]['id'])
    if header_attachments:
        full_description += '\n__________________________\n\n'
        full_description += '\n'.join(header_attachments)

    vlog("Ticket Info: %s" % (full_description,))
    ticket = phab.maniphest.createtask(title=rtinfo['Subject'],
                                       description=full_description,
                                       projectPHIDs=phids,
                                       ccPHIDs=[],
                                       priority=rtinfo['xpriority'],
                                       auxiliary={"std:maniphest:external_reference":"rt%s" % (rtid,)})

    # XXX: perms
    botphid = phabdb.get_phid_by_username(config.phab_user)
    phabdb.set_task_title_transaction(ticket['phid'],
                                      botphid,
                                      'public',
                                      'public')

    phabdb.set_task_ctime(ticket['phid'], rtlib.str_to_epoch(rtinfo['Created']))
    phabdb.set_task_policy(ticket['phid'], viewpolicy)

    vlog(str(ordered_comments))
    fmt_comments = {}
    for comment, contents in comment_dict.iteritems():
        fmt_comment = {}
        dbody = contents['body']
        if dbody['content'] is None and dbody['creator'] is None:
            continue
        elif dbody['content'] is None:
            content = 'no content found'
        else:
            mailsan = rtlib.shadow_emails(dbody['content'][0])
            content_literal = []
            for c in mailsan.splitlines():
                if c.strip() and not c.lstrip().startswith('>'):
                    # in remarkup having '--' on a new line seems to bold last
                    # line so signatures really cause issues
                    if c.strip() == '--':
                        content_literal.append('%%%{0}%%%'.format(c.strip()))
                    else:
                        content_literal.append(unicode('{0}'.format(c.strip())))
                elif c.strip():
                    content_literal.append(c.strip())
                else:
                    vlog("ignoring content line %s" % (c,))
            content = '\n'.join(content_literal)

        if 'This transaction appears to have no content' in content:
            content = None

        auto_actions = ['Outgoing email about a comment recorded by RT_System',
                        'Outgoing email recorded by RT_System']

        if dbody['description'] in auto_actions:
            vlog("ignoring comment: %s/%s" % (dbody['description'], content))
            continue

        preamble = unicode('')
        cbody = unicode('')
        if content:
            preamble += unicode("`%s  wrote:`\n\n" % (dbody['creator'].strip(),))
            cbody += unicode(content).strip() or 'no content'
        if dbody['nvalue'] or dbody['ovalue']:
            value_update = ''
            value_update_text = rtlib.shadow_emails(dbody['description'])
            value_update_text = value_update_text.replace('fsck.com-rt', 'https')
            relations = ['Reference by ticket',
                         'Dependency by',
                         'Reference to ticket',
                         'Dependency on',
                         'Merged into ticket',
                         'Membership in']

            states = ['open', 'resolved', 'new', 'stalled']
            if any(map(lambda x: x in dbody['description'], relations)):
                value_update = value_update_text
            elif re.search('tags\s\S+\sadded', dbody['description']):
                value_update = "%s added tag %s" % (dbody['creator'], dbody['nvalue'])
            elif re.search('Taken\sby\s\S+', dbody['description']):
                value_update = "Issue taken by **%s**" % (dbody['creator'],)
            else:
                value_update = "//%s//" % (value_update_text,)
            cbody += value_update

        afound = contents['body']['attached']
        cbody_attachments = []
        for a in afound:
            if a in upfiles:
                cbody_attachments.append('{F%s}' % uploaded[a]['id'])
        if cbody_attachments:
            cbody += '\n__________________________\n\n'
            cbody += '\n'.join(cbody_attachments)
            fmt_comment['xattached'] = cbody_attachments

        phabm.task_comment(ticket['id'], preamble + cbody)
        ctransaction = phabdb.last_comment(ticket['phid'])
        created = rtlib.str_to_epoch_comments(dbody['created'])
        phabdb.set_comment_time(ctransaction,
                                created)
        fmt_comment['xctransaction'] = ctransaction
        fmt_comment['preamble'] = preamble
        fmt_comment['content'] = cbody
        fmt_comment['created'] = created
        # XXX TRX both ways?
        #fmt_comment['creator'] = dbody['creator']user_lookup(name)
        fmt_comments[created] = fmt_comment

    if rtinfo['Status'].lower() != 'open':
        log('setting %s to status %s' % (rtid, rtinfo['xstatus'].lower()))
        phabdb.set_issue_status(ticket['phid'], rtinfo['xstatus'].lower())

    log("Created task: T%s (%s)" % (ticket['id'], ticket['phid']))
    phabdb.set_task_mtime(ticket['phid'], rtlib.str_to_epoch(rtinfo['LastUpdated']))
    xcomments = json.dumps(fmt_comments)
    pmig.sql_x("UPDATE rt_meta SET xcomments=%s WHERE id = %s", (xcomments, rtid))
    pmig.sql_x("UPDATE rt_meta SET priority=%s, modified=%s WHERE id = %s",
               (ipriority['creation_success'], now(), rtid))
    pmig.close()
    return True


def run_create(rtid, tries=1):
    if tries == 0:
        pmig = phabdb.phdb(db=config.rtmigrate_db)
        import_priority = pmig.sql_x("SELECT priority \
                                      FROM rt_meta \
                                      WHERE id = %s", \
                                      (rtid,))
        if import_priority:
            pmig.sql_x("UPDATE rt_meta \
                       SET priority=%s, modified=%s \
                       WHERE id = %s",
                       (ipriority['creation_failed'],
                       now(),
                       rtid))
        else:
            elog("%s does not seem to exist" % (rtid))
        elog('failed to create %s' % (rtid,))
        pmig.close()
        return False
    try:
        return create(rtid)
    except Exception as e:
        import traceback
        tries -= 1
        time.sleep(5)
        traceback.print_exc(file=sys.stdout)
        elog('failed to grab %s (%s)' % (rtid, e))
        return run_create(rtid, tries=tries)

def main():

    if not util.can_edit_ref:
        elog('%s reference field not editable on this install' % (rtid,))
        sys.exit(1)

    if 'failed' in sys.argv:
        priority = ipriority['creation_failed']
    elif 'success' in sys.argv:
        priority = ipriority['creation_success']
    else:
        priority = None

    vlog("Grabbing for priority: %s" % (priority,))
    pmig = phdb(db=config.rtmigrate_db)
    bugs = return_bug_list(dbcon=pmig,
                           priority=priority,
                           table='rt_meta')
    pmig.close()

    #Serious business
    if 'failed' in sys.argv or '-r' in sys.argv:
        for b in bugs:
            util.notice("Removing rtid %s" % (b,))
            log(util.remove_issue_by_bugid(b, rtlib.prepend))

    from multiprocessing import Pool
    pool = Pool(processes=int(config.bz_createmulti))
    _ =  pool.map(run_create, bugs)
    complete = len(filter(bool, _))
    failed = len(_) - complete
    print '%s completed %s, failed %s' % (sys.argv[0], complete, failed)

if __name__ == '__main__':
    main()