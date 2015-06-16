from urllib import urlencode
import json
import isodate

# KA: swapped urlfetch in for requests in order to play nice w/ App Engine
from google.appengine.api import urlfetch


def get_class(str_or_class):
    """Accept a name or actual class object for a class in the current module.
    Return a class object."""
    if isinstance(str_or_class, str):
        return globals()[str_or_class]
    else:
        return str_or_class


class TrelloConnection(object):

    def __init__(self, api_key, oauth_token):
        self.key = api_key
        self.token = oauth_token

    def request(self, method, path, params=None, body=None, filename=None):

        if not path.startswith('/'):
            path = '/' + path
        url = 'https://api.trello.com/1' + path

        params = params or {}
        params.update({'key': self.key, 'token': self.token})
        url += '?' + urlencode(params)

        # KA: doesn't currently support file uploads. This was removed when we
        # swapped urlfetch in for requests, and we'll add it back in if/when
        # we need it.
        response = urlfetch.fetch(url=url, method=method)
        if response.status_code != 200:
            raise Exception("Error: (%s) \"%s\"" %
                    (response.status_code, response.content))
        return response.content

    def get(self, path, params=None):
        return self.request('GET', path, params)

    def post(self, path, params=None, body=None):
        return self.request('POST', path, params, body)

    def put(self, path, params=None, body=None):
        return self.request('PUT', path, params, body)

    def delete(self, path, params=None, body=None):
        return self.request('DELETE', path, params, body)

    def get_board(self, board_id):
        return Board(self, board_id)

    def get_card(self, card_id):
        return Card(self, card_id)

    def get_list(self, list_id):
        return List(self, list_id)

    def get_checklist(self, checklist_id):
        return Checklist(self, checklist_id)

    def get_member(self, member_id):
        return Member(self, member_id)

    def get_notification(self, not_id):
        return Notification(self, not_id)

    def get_organization(self, org_id):
        return Organization(self, org_id)

    def get_token(self, token):
        return Token(self, token)

    @property
    def me(self):
        """
        Return a Membership object for the user whose credentials were used to
        connect.
        """
        return Member(self, 'me')


class Closable(object):
    """
    Mixin for Trello objects for which you're allowed to PUT to <id>/closed.
    """
    def close(self):
        path = self._prefix + self._id + '/closed'
        params = {'value': 'true'}
        result = self._conn.put(path, params=params)


class Deletable(object):
    """
    Mixin for Trello objects which are allowed to be DELETEd.
    """
    def delete(self):
        path = self._prefix + self._id
        self._conn.delete(path)


class WebHookable(object):
    """
    Mixin for Trello objects which can have webhooks attached to 'em.
    """
    def add_webhook(self, callbackURL):
        """Attach a webhook to this object which hits callbackURL on events."""
        path = WebHook._prefix
        params = {'idModel': self._id, 'callbackURL': callbackURL}
        data = json.loads(self._conn.post(path, params=params))
        webhook = WebHook(self._conn, data['id'], data)
        return webhook


class Labeled(object):
    """
    Mixin for Trello objects which have labels.
    """

    # TODO: instead of set_label and get_label, just override the 'labels'
    #  property to call set and get as appropriate.

    _valid_label_colors = [
        'green',
        'yellow',
        'orange',
        'red',
        'purple',
        'blue',
    ]

    def set_label(self, color):
        color = color.lower()
        if color not in self._valid_label_colors:
            raise ValueError("invalid color")
        path = self._prefix + self._id + '/labels'
        params = {'value': color}
        self._conn.post(path, params=params)

    def clear_label(self, color):
        color = color.lower()
        if color not in self._valid_label_colors:
            raise ValueError("invalid color")
        path = self._prefix + self._id + '/labels/' + color
        self._conn.delete(path)


class Field(object):
    """
    A simple field on a Trello object.  Maps the attribute to a key in the
    object's _data dict.
    """

    def __init__(self, key=None):
        self.key = key

    def __get__(self, instance, owner):
        # Accessing instance._data will trigger a fetch from Trello if the
        # _data attribute isn't already present.
        return instance._data[self.key]


class DateField(Field):
    def __get__(self, instance, owner):
        raw = super(DateField, self).__get__(instance, owner)
        return isodate.parse_datetime(raw)

class IntField(Field):
    def __get__(self, instance, owner):
        raw = super(IntField, self).__get__(instance, owner)
        return int(raw)

class BoolField(Field):
    def __get__(self, instance, owner):
        raw = super(BoolField, self).__get__(instance, owner)
        return bool(raw)


class ObjectField(Field):
    """
    Maps an idSomething string attr on an object to another object type.
    """

    def __init__(self, key, cls):

        self.key = key
        self.cls = cls

    def __get__(self, instance, owner):
        return self.related_instance(instance._conn, instance._data[self.key])

    def related_instance(self, conn, obj_id):
        return get_class(self.cls)(conn, obj_id)


class ListField(ObjectField):
    """
    Like an ObjectField, but a list of them.  For fleshing out things like
    idMembers.
    """

    def __get__(self, instance, owner):
        ids = instance._data[self.key]
        conn = instance._conn
        return [self.related_instance(conn, id) for id in ids]


class SubList(object):
    """
    Kinda like a ListField, but for things listed under a URL subpath (like
    /boards/<id>/cards), as opposed to a list of ids in the document body
    itself.
    """

    def __init__(self, cls):
        # cls may be a name of a class, or the class itself
        self.cls = cls

        # A dict of sublists, by instance id
        self._lists = {}

    def __get__(self, instance, owner):
        # KA: trollop's SubList does some dangerous caching on the SubList
        # class. This can result in stale data reads. We're working around the
        # bug and not trying to refactor trollop's code by making the cache key
        # use both the Trello object's instance id and the python object's
        # identity according to id().
        list_id = "%s:%s" % (instance._id, id(instance))
        if not list_id in self._lists:
            cls = get_class(self.cls)
            path = instance._prefix + instance._id + cls._prefix
            data = json.loads(instance._conn.get(path))
            self._lists[list_id] = [cls(instance._conn, d['id'], d) for d in data]
        return self._lists[list_id]


class TrelloMeta(type):
    """
    Metaclass for LazyTrello objects, allowing documents to have Field
    attributes that know their names without them having to be explicitly
    passed to __init__.
    """
    def __new__(cls, name, bases, dct):
        for k, v in dct.items():
            # For every Field on the class that wasn't initted with an explicit
            # 'key', set the field name as the key.
            if isinstance(v, Field) and v.key is None:
                v.key = k
        return super(TrelloMeta, cls).__new__(cls, name, bases, dct)


class LazyTrello(object):
    """
    Parent class for Trello objects (cards, lists, boards, members, etc).  This
    should always be subclassed, never used directly.
    """

    __metaclass__ = TrelloMeta

    # The Trello API path where objects of this type may be found. eg '/cards/'
    @property
    def _prefix(self):
        raise NotImplementedError, "LazyTrello subclasses MUST define a _prefix"

    def __init__(self, conn, obj_id, data=None):
        self._id = obj_id
        self._conn = conn
        self._path = self._prefix + obj_id

        # If we've been passed the data, then remember it and don't bother
        # fetching later.
        if data:
            self._data = data

    def __getattr__(self, attr):
        if attr == '_data':
            # Something is trying to access the _data attribute.  If we haven't
            # fetched data from Trello yet, do so now.  Cache the result on the
            # object.
            if not '_data' in self.__dict__:
                self._data = json.loads(self._conn.get(self._path))

            return self._data
        else:
            raise AttributeError("%r object has no attribute %r" %
                                 (type(self).__name__, attr))

    def __unicode__(self):
        tmpl = u'<%(cls)s: %(name_or_id)s>'
        # If I have a name, use that
        if 'name' in self._data:
            return tmpl % {'cls': self.__class__.__name__,
                           'name_or_id': self._data['name']}

        return tmpl % {'cls': self.__class__.__name__,
                       'name_or_id': self._id}

    def __str__(self):
        return str(self.__unicode__())

    def __repr__(self):
        return str(self.__unicode__())

### BEGIN ACTUAL WRAPPER OBJECTS


class Action(LazyTrello):

    _prefix = '/actions/'
    data = Field()
    type = Field()
    date = DateField()
    creator = ObjectField('idMemberCreator', 'Member')


class Board(LazyTrello, Closable, WebHookable):

    _prefix = '/boards/'

    url = Field()
    name = Field()
    pinned = Field()
    prefs = Field()
    desc = Field()
    closed = Field()

    organization = ObjectField('idOrganization', 'Organization')

    actions = SubList('Action')
    cards = SubList('Card')
    checklists = SubList('Checklist')
    lists = SubList('List')
    members = SubList('Member')


class Card(LazyTrello, Closable, Deletable, Labeled):

    _prefix = '/cards/'

    url = Field()
    closed = Field()
    name = Field()
    badges = Field()
    checkItemStates = Field()
    desc = Field()
    labels = Field()

    board = ObjectField('idBoard', 'Board')
    list = ObjectField('idList', 'List')
    stickers = SubList('Sticker')
    attachments = SubList('Attachment')

    checklists = ListField('idChecklists','Checklist')
    members = ListField('idMembers', 'Member')

    def detach(self, attachment):
        """
        Remove attachment from card
        """
        assert isinstance(attachment, Attachment)
        path = self._path + attachment._path
        self._conn.delete(path)

    def attach(self, name, file):
        """
        Create new attachment from the open 'file' and name it 'name'.
        """
        path = self._path + '/attachments'
        return self._conn.request('POST', path, body=file, filename=name)

    def set_cover(self, attachment):
        """
        Set attachment as card cover.
        If attachment is None, remove it.
        """
        path = self._path + '/idAttachmentCover'
        if attachment:
            self._conn.put(path, dict(value=attachment._id))
        else:
            self._conn.put(path, dict(value=''))

    def paste_sticker(self, name, position, rotate=None):
        """
        Paste a sticker to a card.
        position is (x,y,z) where x,y is the top-left corner
        and z is the layer index (integer)
        """
        x,y,z = position
        params = dict(image= name,
                    top=y, left=x, zIndex=z)
        if rotate is not None:
            params['rotate'] = rotate
        path = self._path + '/stickers'
        self._conn.post(path, params)

    def remove_sticker(self, sticker):
        """
        Remove a stricker from a card
        """
        path = self._path + '/stickers/' + sticker._id
        self._conn.delete(path)

    def add_comment(self, text):
        """
        Add a comment to a card
        """
        path = self._path + '/actions/comments'
        return self._conn.post(path, dict(text=text))

    def remove_comment(self, idAction):
        pass



class Checklist(LazyTrello):

    _prefix = '/checklists/'

    checkItems = SubList('CheckItem')
    name = Field()
    board = ObjectField('idBoard', 'Board')
    cards = SubList('Card')

    # TODO: provide a nicer API for checkitems.  Figure out where they're
    # marked as checked or not.

    # TODO: Figure out why checklists have a /cards/ subpath in the docs.  How
    # could one checklist belong to multiple cards?

class CheckItem(LazyTrello):

    _prefix = '/checkItems/'

    name = Field()
    pos = Field()
    type = Field()

class List(LazyTrello, Closable):

    _prefix = '/lists/'

    closed = Field()
    name = Field()
    url = Field()
    board = ObjectField('idBoard', 'Board')
    cards = SubList('Card')

    # TODO: Generalize this pattern, add it to a base class, and make it work
    # correctly with SubList
    def add_card(self, name, desc=None):
        path = self._prefix + self._id + '/cards'
        body = json.dumps({'name': name, 'idList': self._id, 'desc': desc,
                           'key': self._conn.key, 'token': self._conn.token})
        data = json.loads(self._conn.post(path, body=body))
        card = Card(self._conn, data['id'], data)
        return card

class Sticker(LazyTrello):
    _prefix = '/stickers/'

    image = Field()
    imageUrl = Field()


class CustomSticker(LazyTrello):
    _prefix = '/customStickers/'

    url = Field()


class WebHook(LazyTrello, Deletable):
    _prefix = '/webhooks/'

    active = BoolField()
    callbackURL = Field()
    description = Field()
    idModel = Field()


class Attachment(LazyTrello):
    # deletable through card
    _prefix = '/attachments/'

    bytes = IntField()
    date = DateField()
    mimeType = Field()
    name = Field()
    url = Field()
    isUpload = BoolField()



class Member(LazyTrello):

    _prefix = '/members/'

    url = Field()
    fullname = Field('fullName')
    username = Field()

    actions = SubList('Action')
    boards = SubList('Board')
    cards = SubList('Card')
    notifications = SubList('Notification')
    organizations = SubList('Organization')
    customStickers = SubList('CustomSticker')


class Token(LazyTrello):

    _prefix = '/tokens/'

    webhooks = SubList('WebHook')


class Notification(LazyTrello):

    _prefix = '/notifications/'

    data = Field()
    date = DateField()
    type = Field()
    unread = Field()

    creator = ObjectField('idMemberCreator', 'Member')


class Organization(LazyTrello):

    _prefix = '/organizations/'

    url = Field()
    desc = Field()
    displayname = Field('displayName')
    name = Field()

    actions = SubList('Action')
    boards = SubList('Board')
    members = SubList('Member')
