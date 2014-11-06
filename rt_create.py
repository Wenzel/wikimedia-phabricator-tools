#!/usr/bin/env python
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
        #return True

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
    for k, v in ainfo_f.iteritems():
        # Handle general attachment case:
        # NO: 686318802.html (application/octet-stream / 19.5k),
        # YES: Summary_686318802.pdf (application/unknown / 215.3k),
        extract = re.search('(.*)\.(\S{3,4})\s\((.*)\s\/\s(.*)\)', v)
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
        if not extract and v.startswith('Attached Message Part'):
            continue
        elif not extract:
           raise Exception("no attachment extraction: %s %s (%s)" % (k, v, rtid))
           continue
        else:
           vlog(extract.groups())
           ainfo_ext[k] = extract.groups()

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
        vcontent = response.get(path=full, headers={'Content-Type': v[2], 'Content-Length': v[3] })
        #PDF's don't react well to stripping header -- fine without it
        if file_extension.strip() == 'pdf':
            sanscontent = ''.join(vcontent.readlines())
        else:
            vcontent = vcontent.readlines()
            sanscontent = ''.join(vcontent[2:])

        #{u'mimeType': u'image/jpeg', u'authorPHID': u'PHID-USER-bn2kbod4i7geycrbicns', 
        #u'phid': u'PHID-FILE-ioj2mrujudkrekhl5pkl', u'name': u'0jp9B09.jpg',
        #u'objectName': u'F25786', u'byteSize': u'120305',
        #u'uri': u'http://fabapitest.wmflabs.org/file/data/t7j2qp7l5z4ou5qpbx2u/PHID-FILE-ioj2mrujudkrekhl5pkl/0jp9B09.jpg',
        #u'dateCreated': u'1409345752', u'dateModified': u'1409345752', u'id': u'25786'}
        upload = phabm.upload_file("%s.%s" % (v[0], file_extension), sanscontent)
        uploaded[k] = upload

    ptags = []
    ptags.append(rtinfo['Queue'])

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
    ticket =  phab.maniphest.createtask(title=rtinfo['Subject'],
                                        description=full_description,
                                        projectPHIDs=phids,
                                        ccPHIDs=[],
                                        priority=rtinfo['xpriority'],
                                        auxiliary={"std:maniphest:external_reference":"rt%s" % (rtid,),
                                                   "std:maniphest:security_topic":"%s" % ('none')})

    phabdb.set_task_ctime(ticket['phid'], rtlib.str_to_epoch(rtinfo['Created']))

    vlog(str(ordered_comments))
    for comment, contents in comment_dict.iteritems():
        dbody = contents['body']

        if dbody['content'] is None and dbody['creator'] is None:
            continue
        elif dbody['content'] is None:
            content = 'no content found'
        else:
            # peel off email signatures
            #if 'Forwarded message' in dbody['content'][0]:
            #    nosig = rtlib.shadow_emails(dbody['content'][0])
            #else:
            #nosig = remove_sig(dbody['content'][0])
            mailsan = rtlib.shadow_emails(dbody['content'][0])
            content_literal = []
            for c in mailsan.splitlines():
                if c.strip() and not c.lstrip().startswith('>'):
                    # in remarkup having '--' on a new line seems to bold last
                    # line so signatures really cause issues
                    if c.strip() == '--':
                        content_literal.append('%%%{0}%%%'.format(c.strip()))
                    else:
                        content_literal.append('{0}'.format(c.strip()))
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

        #cbody = "`%s`" % (dbody['description'],)
        #cbody += '\n________________\n'

        #if dbody['creator'] == 'RT_System':
        #else:
        #    cbody = "`%s`\n" % (dbody['description'],)
        #cbody = "**%s** on `%s`\n\n**Description**: %s\n **Message**: %s\n" % (dbody['creator'],
        #                                  dbody['created'],
        #                                  content or 'no message')

        cbody = ''
        if content:
            cbody += "`%s  wrote:`\n" % (dbody['creator'].strip(),)
            cbody += "\n%s" % (content.strip() or 'no content',)

        
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
            #    value_update = dbody['description'].replace('fsck.com-rt', 'https')

            #    value_update += "%s added reference %s" % (dbody['creator'], ref)
            #elif 'Dependency by' in dbody['description']:
            #    value_update = dbody['description'].strip()
            #    ref = dbody['nvalue'].replace('fsck.com-rt', 'https')
            #    value_update = "%s added depended on by %s" % (dbody['creator'], ref)

            elif re.search('tags\s\S+\sadded', dbody['description']):
                value_update = "%s added tag %s" % (dbody['creator'], dbody['nvalue'])
            elif re.search('Taken\sby\s\S+', dbody['description']):
                value_update = "Issue taken by **%s**" % (dbody['creator'],)
            #elif dbody['nvalue'].strip() or dbody['ovalue'].strip() in states:
            #    print "FOUND IN STATES", dbody['nvalue'], dbody['ovalue']
            #    value_update = "\n**%s** updated values: " % (dbody['creator'],)
            #    value_update += "|**%s** | => |**%s**|" % (dbody['ovalue'] or 'none',
            #                                               dbody['nvalue'] or 'none')
            else:
                value_update = "//%s//" % (value_update_text,)

            #elif dbody['ovalue'].isdigit() and dbody['nvalue'].isdigit():
            #    value_update = "//%s//" % (value_update_text,)
            #elif not dbody['ovalue'].strip() and dbody['nvalue'].isdigit():
            #    value_update = "//%s//" % (value_update_text,)
            #elif not dbody['nvalue'].strip() and dbody['ovalue'].isdigit():
            #    value_update = "//%s//" % (value_update_text,)
            #else:
            #    print dbody['description']
            #    value_update = "\n**%s** updated values: " % (dbody['creator'],)
            #    value_update += "|**%s** | => |**%s**|" % (dbody['ovalue'] or 'none',
            #                                               dbody['nvalue'] or 'none')
            cbody += value_update

        afound = contents['body']['attached']
        cbody_attachments = []
        for a in afound:
            if a in upfiles:
                cbody_attachments.append('{F%s}' % uploaded[a]['id'])
        if cbody_attachments:
            cbody += '\n__________________________\n\n'
            cbody += '\n'.join(cbody_attachments)
        phabm.task_comment(ticket['id'], cbody)

    if rtinfo['Status'].lower() != 'open':
        log('setting %s to status %s' % (rtid, rtinfo['xstatus'].lower()))
        phabdb.set_issue_status(ticket['phid'], rtinfo['xstatus'].lower())
    log("Created task: T%s (%s)" % (ticket['id'], ticket['phid']))
    phabdb.set_task_mtime(ticket['phid'], rtlib.str_to_epoch(rtinfo['LastUpdated']))
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
                       SET priority=%s modified=%s \
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
        elog('%s reference field not editable on this install' % (bugid,))
        sys.exit(1)

    pmig = phdb(db=config.rtmigrate_db)
    bugs = return_bug_list(dbcon=pmig)
    pmig.close()

    #Serious business
    if 'failed' in sys.argv:
        for b in bugs:
            notice("Removing bugid %s" % (b,))
            log(util.remove_issue_by_bugid(b, bzlib.prepend))

    from multiprocessing import Pool
    pool = Pool(processes=int(config.bz_createmulti))
    _ =  pool.map(run_create, bugs)
    complete = len(filter(bool, _))
    failed = len(_) - complete
    print '%s completed %s, failed %s' % (sys.argv[0], complete, failed)

if __name__ == '__main__':
    main()
