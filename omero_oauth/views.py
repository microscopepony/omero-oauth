#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
from datetime import datetime

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.template import loader as template_loader
from django.template import RequestContext as Context
from django.core.urlresolvers import reverse
from django.core.exceptions import PermissionDenied

from requests_oauthlib import OAuth2Session

import omero
from omero.rtypes import unwrap
from omeroweb.decorators import (
    get_client_ip,
    login_required,
    parse_url,
)
from omeroweb.connector import Connector
from omero_version import (
    build_year,
    omero_version,
)

from omeroweb.webclient.webclient_gateway import OmeroWebGateway
from omeroweb.webadmin.webadmin_utils import upgradeCheck

import oauth_settings


logger = logging.getLogger(__name__)

USERAGENT = 'OMERO.oauth'


def index(request, **kwargs):
    """
    Return the signup page if user is not signed in
    """
    r = handle_logged_in(request, **kwargs)
    if r:
        return r
    return handle_not_logged_in(request)


def handle_logged_in(request, **kwargs):
    """
    If logged in redirect to main webclient, otherwise return None
    """
    # Abuse the @login_required decorateor since it contains the
    # methods to check for an existing session
    check = login_required()

    # Copied from
    # https://github.com/openmicroscopy/openmicroscopy/blob/v5.4.10/components/tools/OmeroWeb/omeroweb/decorators.py#L448
    conn = kwargs.get('conn', None)
    server_id = kwargs.get('server_id', None)
    if conn is None:
        try:
            conn = check.get_connection(server_id, request)
        except Exception:
            conn = None
    if conn is not None:
        logger.error('Logged in')
        try:
            url = parse_url(settings.LOGIN_REDIRECT)
        except Exception:
            url = reverse("webindex")
        return HttpResponseRedirect(url)


def handle_not_logged_in(request):
    oauth = OAuth2Session(oauth_settings.OAUTH_CLIENT_ID,
                          scope=oauth_settings.OAUTH_CLIENT_SCOPE)
    authorization_url, state = oauth.authorization_url(
        oauth_settings.OAUTH_URL_AUTHORIZATION)
    # state: used for CSRF protection
    request.session['oauth_state'] = state

    context = {
        'version': omero_version,
        'build_year': build_year,
        'authorization_url': authorization_url,
        'client_name': oauth_settings.OAUTH_CLIENT_NAME
    }
    if hasattr(settings, 'LOGIN_LOGO'):
        context['LOGIN_LOGO'] = settings.LOGIN_LOGO

    t = template_loader.get_template('oauth/index.html')
    c = Context(request, context)
    rsp = t.render(c)
    return HttpResponse(rsp)


def _expand_template(name, args):
    template = getattr(oauth_settings, name)
    return template.format(**args)


def callback(request):
    state = request.session.get('oauth_state')
    if not state:
        raise PermissionDenied('OAuth state missing')
    code = request.GET.get('code')
    if not code:
        raise PermissionDenied('OAuth code missing')

    oauth = OAuth2Session(oauth_settings.OAUTH_CLIENT_ID, state=state)
    token = oauth.fetch_token(
        oauth_settings.OAUTH_URL_TOKEN,
        client_secret=oauth_settings.OAUTH_CLIENT_SECRET,
        code=code)
    logger.debug('Got OAuth token %s', token)
    userinfo = oauth.get(oauth_settings.OAUTH_URL_USERINFO).json()

    omename = _expand_template('OAUTH_USER_NAME', userinfo)
    email = _expand_template('OAUTH_USER_EMAIL', userinfo)
    firstname = _expand_template('OAUTH_USER_FIRSTNAME', userinfo)
    lastname = _expand_template('OAUTH_USER_LASTNAME', userinfo)

    uid, session = get_or_create_account_and_session(
        omename, email, firstname, lastname)
    return login_with_session(request, session)


def get_or_create_account_and_session(omename, email, firstname, lastname):
    adminc = OmeroWebGateway(
        host=oauth_settings.OAUTH_HOST,
        port=oauth_settings.OAUTH_PORT,
        username=oauth_settings.OAUTH_ADMIN_USERNAME,
        passwd=oauth_settings.OAUTH_ADMIN_PASSWORD,
        secure=True)
    if not adminc.connect():
        raise Exception('Failed to get account '
                        '(unable to obtain admin connection)')
    try:
        e = adminc.getObject('Experimenter', attributes={'omeName': omename})
        if e:
            uid = e.id
        else:
            gid = get_or_create_group(adminc)
            uid = create_user(adminc, omename, email, firstname, lastname, gid)
        session = get_session_for_user(adminc, omename)
    finally:
        adminc.close()
    return uid, session


def get_or_create_group(adminc, groupname=None):
    if not groupname:
        groupname = oauth_settings.OAUTH_GROUP_NAME
        if oauth_settings.OAUTH_GROUP_NAME_TEMPLATETIME:
            groupname = datetime.now().strftime(groupname)
    g = adminc.getObject(
        'ExperimenterGroup', attributes={'name': groupname})
    if g:
        gid = g.id
    else:
        logger.info('Creating new oauth group: %s %s', groupname,
                    oauth_settings.OAUTH_GROUP_PERMS)
        # Parent methods BlitzGateway.createGroup is easier to use than
        # the child method
        gid = super(OmeroWebGateway, adminc).createGroup(
            name=groupname, perms=oauth_settings.OAUTH_GROUP_PERMS)
    return gid


def create_user(adminc, omename, email, firstname, lastname, groupid):
    logger.info('Creating new oauth user: %s group: %d', omename, groupid)
    uid = adminc.createExperimenter(
        omeName=omename, firstName=firstname, lastName=lastname,
        email=email, isAdmin=False, isActive=True,
        defaultGroupId=groupid, otherGroupIds=[],
        password=None)
    return uid


def get_session_for_user(adminc, omename):
    # https://github.com/openmicroscopy/openmicroscopy/blob/v5.4.10/examples/OmeroClients/sudo.py
    ss = adminc.c.getSession().getSessionService()
    p = omero.sys.Principal()
    p.name = omename
    # p.group = 'user'
    p.eventType = 'User'
    # http://downloads.openmicroscopy.org/omero/5.4.10/api/slice2html/omero/api/ISession.html#createSessionWithTimeout
    # This is the absolute timeout (relative to creation time)
    user_session = unwrap(ss.createSessionWithTimeout(
        p, oauth_settings.OAUTH_USER_TIMEOUT * 1000).getUuid())
    logger.debug('Created new oauth session: %s %s', omename, user_session)
    return user_session


def test_login(request, username):
    adminc = OmeroWebGateway(
        host=oauth_settings.OAUTH_HOST,
        port=oauth_settings.OAUTH_PORT,
        username=oauth_settings.OAUTH_ADMIN_USERNAME,
        passwd=oauth_settings.OAUTH_ADMIN_PASSWORD,
        secure=True)
    assert adminc.connect()
    session = get_session_for_user(adminc, username)

    logger.info('test_login: %s', session)
    return login_with_session(request, session)


def login_with_session(request, session):
    # Based on
    # https://github.com/openmicroscopy/openmicroscopy/blob/v5.4.10/components/tools/OmeroWeb/omeroweb/webgateway/views.py#L2943
    username = session
    password = session
    server_id = 1
    is_secure = settings.SECURE
    connector = Connector(server_id, is_secure)

    compatible = True
    if settings.CHECK_VERSION:
        compatible = connector.check_version(USERAGENT)
    if compatible:
        conn = connector.create_connection(
            USERAGENT, username, password,
            userip=get_client_ip(request))
        if conn is not None:
            try:
                request.session['connector'] = connector
                # UpgradeCheck URL should be loaded from the server or
                # loaded omero.web.upgrades.url allows to customize web
                # only
                try:
                    upgrades_url = settings.UPGRADES_URL
                except AttributeError:
                    upgrades_url = conn.getUpgradesUrl()
                upgradeCheck(url=upgrades_url)
                # return handle_logged_in(request, conn, connector)
                return handle_logged_in(request, conn=conn)
            finally:
                conn.close(hard=False)

        raise Exception('Failed to login with session %s', session)
    raise Exception('Incompatible server')
