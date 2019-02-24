""" API endpoints. """

from datetime import datetime, timedelta
from flask import Blueprint, jsonify, request, render_template, g, current_app, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_login import login_required, current_user
from flask_oauthlib.provider import OAuth2Provider
from peewee import JOIN
from .. import misc
from ..socketio import socketio
from ..models import Sub, User, Grant, Token, Client, SubPost, Sub, SubPostComment, APIToken, APITokenSettings
from ..models import SiteMetadata, SubPostVote

api = Blueprint('api', __name__)
oauth = OAuth2Provider()


class OAuthClient(object):
    """ Maps DB stuff to something that oauthlib can understand """
    def __init__(self, stuff):
        self.is_confidential = bool(stuff.is_confidential)
        self._redirect_uris = stuff._redirect_uris
        self._default_scopes = stuff._default_scopes
        self.client = stuff.client
        self.client_id = self.client

    @property
    def client_type(self):
        """ Returns the client type """
        if self.is_confidential:
            return 'confidential'
        return 'public'

    @property
    def redirect_uris(self):
        """ Returns all the redirect uris """
        if self._redirect_uris:
            return self._redirect_uris.split()
        return []

    @property
    def default_redirect_uri(self):
        """ returns the default redirect uri """
        return self.redirect_uris[0]

    @property
    def default_scopes(self):
        """ Returns the default scopes for the client """
        if self._default_scopes:
            return self._default_scopes.split()
        return []


class OAuthGrant(object):
    """ Maps grants in DB to stuff oauthlib can use """
    def __init__(self, stuff):
        self.tuff = stuff
        self.redirect_uri = stuff.redirect_uri

    @property
    def user(self):
        """ Returns user info for this grant """
        return self.tuff.user

    @property
    def scopes(self):
        """ Returns grant's scopes """
        if self.tuff._scopes:
            return self.tuff._scopes.split()
        return []

    def delete(self):
        """ Deletes this scope """
        Grant.delete().where(Grant.id == self.tuff.id).execute()


class OAuthToken(object):
    """ Maps DB oauth tokens to oauthlib stuff """
    def __init__(self, stuff):
        self.tuff = stuff
        self.expires = stuff.expires
        self.scopes = stuff._scopes.split()

    @property
    def user(self):
        """ Returns the user this token is attached to """
        return self.tuff.user

    def delete(self):
        """ Deletes this token """
        Token.delete().where(Token.id == self.tuff.id).execute()


# OAuth stuff
@oauth.clientgetter
def load_client(client_id):
    """ Loads OAuth clients """
    try:
        qr = Client.get(Client.client == client_id)
        return OAuthClient(qr)
    except Client.DoesNotExist:
        return None


@oauth.grantgetter
def load_grant(client_id, code):
    """ Gets grants.. """
    try:
        qr = Grant.get((Grant.client == client_id) & (Grant.code == code))
        return OAuthGrant(qr)
    except Grant.DoesNotExist:
        return None


@oauth.grantsetter
def save_grant(client_id, code, req, *args, **kwargs):
    """ Creates grants """
    # decide the expires time yourself
    expires = datetime.utcnow() + timedelta(seconds=100)
    qr = Grant.create(client=client_id, code=code['code'], redirect_uri=req.redirect_uri,
                      _scopes=' '.join(req.scopes), user=current_user.uid, expires=expires)
    qr.save()
    return qr


@oauth.tokengetter
def load_token(access_token=None, refresh_token=None):
    """ Loads oauth token """
    try:
        if access_token:
            qr = Token.get(Token.access_token == access_token)
        elif refresh_token:
            qr = Token.get(Token.refresh_token == refresh_token)
        return OAuthToken(qr)
    except Token.DoesNotExist:
        return None


@oauth.tokensetter
def save_token(token, req, *args, **kwargs):
    """ Creates oauth token """
    Token.delete().where((Token.client == req.client.client) & (Token.user == req.user.uid)).execute()

    expires_in = token.get('expires_in')
    expires = datetime.utcnow() + timedelta(seconds=expires_in)
    qr = Token.create(access_token=token['access_token'], refresh_token=token['refresh_token'], token_type=token['token_type'],
                      _scopes=token['scope'], expires=expires, client_id=req.client.client, user_id=req.user.uid)
    qr.save()
    return qr


@api.route('/oauth/authorize', methods=['GET', 'POST'])
@login_required
@oauth.authorize_handler
def authorize(*args, **kwargs):
    """ The page that authorizes oauth stuff """
    if request.method == 'GET':
        client_id = kwargs.get('client_id')
        client = load_client(client_id)
        kwargs['client'] = client
        kwargs['request'] = request
        # TODO: Make this handle more grants!
        return render_template('oauthorize.html', **kwargs)

    confirm = request.form.get('confirm', 'no')
    return confirm == 'yes'


@api.route('/api/me')
@oauth.require_oauth('account_info')
def me():
    """ Returns basic user info """
    user = request.oauth.user
    return jsonify(email=user.email, username=user.name, uid=user.uid)


@api.route('/api/getToken')
@oauth.require_oauth('interact')
def get_socket_session():
    """ Returns basic user info """
    user = request.oauth.user
    session_serializer = SecureCookieSessionInterface().get_signing_serializer(current_app)

    return jsonify(token=session_serializer.dumps({'uid': user.uid}))


@api.route('/oauth/token', methods=['GET', 'POST'])
@oauth.token_handler
def token_thingy():
    """ Does nothing. """
    return None


def token_required(permission=None):
    """ Checks authorization for token """
    def decorator(f):
        def check_token(*args, **kwargs):
            token = request.values.get('token')
            if not token:
                rj = request.get_json()
                if rj:
                    token = rj.get('token')
                if not token:
                    return jsonify(status='error', error='No token received')
            try:
                tokdata = APIToken.get(APIToken.token == token)
            except APIToken.DoesNotExist:
                return jsonify(status='error', error='Invalid token')
            
            if permission and not getattr(tokdata, permission, False):
                return jsonify(status='error', error='Not authorized')

            if not tokdata.is_active:
                return jsonify(status='error', error='Invalid token')
            
            if tokdata.is_ip_restricted:
                try:
                    APITokenSettings.get((APITokenSettings.token == tokdata) & (APITokenSettings.key == 'authorized_ip') & (APITokenSettings.value == misc.get_ip()))
                except APITokenSettings.DoesNotExist:
                    return jsonify(status='error', error='IP not authorized')
            g.api_token = tokdata

            return f(*args, **kwargs)
        return check_token
    return decorator


# /api/v2

@api.route('/api/getPostList/<target>/<sort>', defaults={'page': 1}, methods=['GET'])
@api.route('/api/getPostList/<target>/<sort>/<int:page>', methods=['GET'])
def getPostList(target, sort, page):
    if sort not in ('hot', 'top', 'new'):
        return jsonify(status="error", error="Invalid sort")
    if page < 1:
        return jsonify(status="error", error="Invalid page number")

    base_query = SubPost.select(SubPost.nsfw, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted, SubPost.score,
                                SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'), SubPost.flair, SubPost.edited,
                                SubPost.comments, SubPost.ptype, User.status.alias('userstatus'), User.uid, SubPost.upvotes, SubPost.downvotes)
    base_query = base_query.join(User, JOIN.LEFT_OUTER).switch(SubPost).join(Sub, JOIN.LEFT_OUTER)

    if target == 'all':
        posts = misc.getPostList(base_query, sort, page).dicts()
    else:
        try:
            sub = Sub.get(Sub.name == target)
        except Sub.DoesNotExist:
            return jsonify(status="error", error="Target does not exist")
        posts = misc.getPostList(base_query.where(Sub.sid == sub.sid), sort, page).dicts()
    
    np = []
    for p in posts:
        if p['userstatus'] == 10:  # account deleted
            p['user'] = '[Deleted]'
            p['uid'] = None
        del p['userstatus']
        np.append(p)

    return jsonify(status='ok', posts=np)


@api.route('/api/getPost/<int:pid>', methods=['GET'])
def getPost(pid):

    base_query = SubPost.select(SubPost.nsfw, SubPost.content, SubPost.pid, SubPost.title, SubPost.posted, SubPost.score, SubPost.deleted,
                                SubPost.thumbnail, SubPost.link, User.name.alias('user'), Sub.name.alias('sub'), SubPost.flair, SubPost.edited,
                                SubPost.comments, SubPost.ptype, User.status.alias('userstatus'), User.uid, SubPost.upvotes, SubPost.downvotes)
    base_query = base_query.join(User, JOIN.LEFT_OUTER).switch(SubPost).join(Sub, JOIN.LEFT_OUTER)

    post = base_query.where(SubPost.pid == pid).dicts()

    if len(post) == 0:
        return jsonify(status="error", error="Post does not exist")
    
    post = post[0]
    post['deleted'] = True if post['deleted'] != 0 else False
    
    if post['deleted']:  # Clear data for deleted posts
        post['content'] = None
        post['link'] = None
        post['uid'] = None
        post['user'] = '[Deleted]'
        post['thumbnail'] = None
        post['edited'] = None
    
    if post['userstatus'] == 10:
        post['user'] = '[Deleted]'
        post['uid'] = None
    del post['userstatus']

    return jsonify(status='ok', post=post)


@api.route("/api/getPostComments/<int:pid>", defaults={'page': 1}, methods=['GET'])
@api.route("/api/getPostComments/<int:pid>/<int:page>", methods=['GET'])
def getComments(pid, page):
    try:
        post = SubPost.get(SubPost.pid == pid)
    except SubPost.DoesNotExist:
        return jsonify(status="error", error="Post does not exist")
    
    comments = SubPostComment.select(SubPostComment.cid, SubPostComment.parentcid, SubPostComment.time.alias('posted'), SubPostComment.lastedit,
                                       SubPostComment.content, SubPostComment.status, SubPostComment.status, User.name.alias('user'),
                                       User.status.alias('userstatus'), SubPostComment.score)
    comments = comments.join(User, JOIN.LEFT_OUTER).where(SubPostComment.pid == pid).paginate(page, 50).dicts()

    for comm in comments:
        if comm['userstatus'] == 10 or comm['status'] not in (0, None):
            comm['user'] = '[Deleted]'
        
        if comm['status'] not in (0, None):
            comm['deleted'] = True
            comm['content'] = None
        else:
            comm['deleted'] = False

        del comm['userstatus']
        del comm['status']

    return jsonify(status="ok", comments=list(comments))


def get_post_data():
    req_json = request.get_json()
    if req_json:
        return req_json
    else:
        return request.form

def api_over_limit(limit):
    return jsonify(status='error', error='Rate limited')


@api.route("/api/createPost", methods=['POST'])
@token_required('can_post')
@misc.ratelimit(1, per=30, over_limit=api_over_limit)
def createPost():
    """ Creates a text post. Parameters taken:
     - ptype: Post type, either "link" or "text"
     - sub: Target sub name.
     - title: Post title.
     - link: post's link (optional)
     - content: Post content (optional)
     - nsfw: true if post is nsfw (optional)
     """
    post_data = get_post_data()
    if not post_data.get('sub') or not post_data.get('title') or not post_data.get('ptype'):
        return jsonify(status='error', error='Missing parameters')
    ptype = post_data.get('ptype')
    if ptype not in ('link', 'text'):
        return jsonify(status='error', error='Invalid ptype')
    # TODO: Unify all checks with do.create_post
    try:
        enable_posting = SiteMetadata.get(SiteMetadata.key == 'enable_posting')
        if enable_posting.value in ('False', '0'):
            return jsonify(status='error', error='Posting has ben temporarily disabled')
    except SiteMetadata.DoesNotExist:
        pass
    
    try:
        sub = Sub.get(Sub.name == post_data['sub'])
    except Sub.DoesNotExist:
        return jsonify(status='error', error='Sub does not exist')

    subdata = misc.getSubData(sub.sid, simple=True)
    if g.api_token.user.uid in subdata['ban']:
        return jsonify(status='error', error='You\'re banned from this sub')
    
    if subdata['restricted'] == '1':
        return jsonify(status='error', error='Restricted sub')
    
    if len(post_data['title'].strip(misc.WHITESPACE)) < 3:
        return jsonify(status='error', error='Post title too short')
    
    if ptype == "link":
        if not post_data.get('link'):
            return jsonify(status='error', error='No link provided')
        
        if post_data.get('content'):
            return jsonify(status='error', error='Link posts do not accept content')
        
        if misc.is_domain_banned(post_data.get('link')):
            return jsonify(status='error', error='Domain is banned')
        
        img = misc.get_thumbnail(post_data.get('link'))
        ptype = 1
    elif ptype == 'text':
        if post_data.get('link'):
            return jsonify(status='error', error='Text posts do not accept link')

        ptype = 0
    
    post = SubPost.create(sid=sub.sid,
                          uid=g.api_token.uid,
                          title=post_data['title'].strip(misc.WHITESPACE),
                          content=post_data.get('content', ''),
                          link=post_data['link'] if ptype == 1 else None,
                          posted=datetime.utcnow(),
                          score=1, upvotes=1, downvotes=0, deleted=0, comments=0,
                          ptype=ptype,
                          nsfw=post_data.get('nsfw', 0) if not subdata['nsfw'] else 1,
                          thumbnail=img if ptype == 1 else '')

    Sub.update(posts=Sub.posts + 1).where(Sub.sid == sub.sid).execute()
    addr = url_for('sub.view_post', sub=sub.name, pid=post.pid)
    posts = misc.getPostList(misc.postListQueryBase(nofilter=True).where(SubPost.pid == post.pid), 'new', 1).dicts()
    socketio.emit('thread',
                  {'addr': addr, 'sub': sub.name, 'type': ptype,
                   'user': g.api_token.user.name, 'pid': post.pid, 'sid': sub.sid,
                   'html': misc.engine.get_template('shared/post.html').render({'posts': posts, 'sub': False})},
                   namespace='/snt', room='/all/new')

    SubPostVote.create(uid=g.api_token.uid, pid=post.pid, positive=True)
    User.update(given=User.given + 1).where(User.uid == g.api_token.uid).execute()

    misc.workWithMentions(post_data.get('content', ''), None, post, sub)
    misc.workWithMentions(post_data.get('title'), None, post, sub)

    socketio.emit('yourvote', {'pid': post.pid, 'status': 1, 'score': post.score}, namespace='/snt',
                  room='user' + g.api_token.user.name)


    return jsonify(status='ok', pid=post.pid)