"""Who and where, for the audit page: the person (with their Feishu/DingTalk nickname) and the chat they acted in.

The audit table only stores internal ids. Everything here is derived at read time from what the row itself points at
(a run, a conversation or a group), in a fixed number of batched queries per page, and never from anything the viewer
could not already see: a group name is shown only if the viewer may read that group.
"""
from sqlalchemy import select

from . import policy
from .models import Conversation, Group, IMDiscovery, IMEvent, Identity, Run, User

PROVIDERS = ('feishu', 'dingtalk')
ID_LIMIT = 64


def identifier(value):
    # Only plausible ids are looked up; details is free-form JSON and may hold anything.
    return value if isinstance(value, str) and 0 < len(value) <= ID_LIMIT else None


def pointers(row):
    """(run id, conversation id, group id, channel) that the row itself names; each may be None."""
    details = row.details if isinstance(row.details, dict) else {}
    action = row.action or ''
    run = identifier(row.target_id) if action.startswith('run.') else identifier(details.get('run_id'))
    conversation = identifier(row.target_id) if action.startswith('conversation.') else None
    group = identifier(row.target_id) if action == 'im.group.bind' else identifier(details.get('group_id'))
    channel = row.target_id if action.startswith('platform.') and row.target_id in PROVIDERS else None
    return run, conversation, group, channel


def may_name(db, viewer, group):
    if group.archived_at is None:
        return policy.can_read_group(db, viewer, group)
    # Archived groups fail the membership and management checks by design, yet the trail must still say where something
    # happened: a manager may name them within their own organization (and team).
    return bool(viewer.active and viewer.role != 'member' and (viewer.role == 'super_admin' or (
        viewer.org_id == group.org_id and (viewer.role != 'team_lead' or viewer.team_id == group.team_id))))


def describe(db, viewer, rows):
    """{audit id: context} for the given rows, using a handful of queries however many rows there are."""
    rows = list(rows)
    found = {row.id: pointers(row) for row in rows}
    run_ids = {run for run, _, _, _ in found.values() if run}
    runs = {r.id: r.conversation_id for r in db.scalars(select(Run).where(Run.id.in_(run_ids)))} if run_ids else {}
    channel_of = {}
    if runs:
        for event in db.scalars(select(IMEvent).where(IMEvent.run_id.in_(set(runs))).order_by(IMEvent.created_at.desc())):
            channel_of.setdefault(event.run_id, event.provider)
    conversation_ids = {conversation for _, conversation, _, _ in found.values() if conversation} | set(runs.values())
    conversations = {c.id: c.group_id for c in db.scalars(select(Conversation).where(Conversation.id.in_(conversation_ids)))} \
        if conversation_ids else {}

    placed = {}
    for row in rows:
        run, conversation, group, channel = found[row.id]
        conversation = conversation or runs.get(run)
        known = conversation in conversations
        group = group or (conversations.get(conversation) if known else None)
        if run in runs:
            # A task from a chat platform has an inbound event; anything else was started on the web.
            channel = channel_of.get(run, 'web')
        placed[row.id] = (channel, group, known and conversations[conversation] is None)

    group_ids = {group for _, group, _ in placed.values() if group}
    groups = {g.id: g for g in db.scalars(select(Group).where(Group.id.in_(group_ids)))} if group_ids else {}
    actor_ids = {row.actor_id for row in rows if row.actor_id}
    # The group policy looks up every member with db.get; loading them (and the actors) in one query up front makes
    # those lookups hit the session instead of costing a query per member.
    wanted = actor_ids | {member for g in groups.values() for member in g.member_ids if isinstance(member, str)}
    loaded = list(db.scalars(select(User).where(User.id.in_(wanted)))) if wanted else []
    users = {u.id: u.name for u in loaded if u.id in actor_ids}
    readable = {key: may_name(db, viewer, group) for key, group in groups.items()}
    nicknames = nickname_of(db, {(row.actor_id, placed[row.id][0]) for row in rows
                                 if row.actor_id in users and placed[row.id][0] in PROVIDERS})

    result = {}
    for row in rows:
        channel, group, private = placed[row.id]
        chat = None
        if group:
            record = groups.get(group)
            chat = {'name': record.name if record and readable[group] else None,
                    'state': 'missing' if record is None else 'ok' if readable[group] else 'hidden',
                    'archived': bool(record and record.archived_at is not None and readable[group])}
        result[row.id] = {'actor_name': users.get(row.actor_id), 'channel': channel,
                          'nickname': nicknames.get((row.actor_id, channel)), 'group': chat,
                          'private': bool(private and channel in PROVIDERS)}
    return result


def nickname_of(db, wanted):
    """{(user id, provider): nickname} for the people's own chat-platform identities; the newest known name wins."""
    if not wanted:
        return {}
    identities = list(db.scalars(select(Identity).where(Identity.user_id.in_({user for user, _ in wanted}),
                                                         Identity.provider.in_(PROVIDERS))))
    senders = {identity.external_user_id for identity in identities}
    names = {}
    if senders:
        known = select(IMDiscovery).where(IMDiscovery.sender_id.in_(senders), IMDiscovery.nickname.is_not(None),
                                          IMDiscovery.provider.in_(PROVIDERS)).order_by(IMDiscovery.last_seen.desc())
        for discovery in db.scalars(known):
            names.setdefault((discovery.provider, discovery.sender_id), discovery.nickname)
    result = {}
    for identity in identities:
        key = (identity.user_id, identity.provider)
        if key in wanted and (identity.provider, identity.external_user_id) in names:
            result.setdefault(key, names[(identity.provider, identity.external_user_id)])
    return result
