import json
from test_api import client, configured, login, PASSWORD

def setup_grant(client):
    member = client.post('/api/v1/users', json={'username':'editor-user','display_name':'Editor','password':PASSWORD}).json()
    other = client.post('/api/v1/users', json={'username':'other-user','display_name':'Other','password':PASSWORD}).json()
    first=client.post('/api/v1/assistants',json={'name':'Industrial','scope':'team'}).json()
    second=client.post('/api/v1/assistants',json={'name':'Other team assistant','scope':'team'}).json()
    client.app.state.service.db.execute('UPDATE assistants SET editor_user_ids=? WHERE id=?',(json.dumps([member['id']]),first['id']))
    login(client,'editor-user')
    return member,other,first,second

def test_grant_edits_only_named_assistant_and_preserves_member_role(client):
    member,_,first,second=setup_grant(client)
    response=client.patch('/api/v1/assistants/'+first['id'],json={'description':'Updated process','expected_revision':first['revision']})
    assert response.status_code==200 and response.json()['can_edit']
    assert response.json()['description']=='Updated process'
    assert client.get('/api/v1/auth/me').json()['role']=='member'
    assert client.patch('/api/v1/assistants/'+second['id'],json={'name':'Stolen'}).status_code==403
    assert client.get('/api/v1/users').status_code==403
    assert client.post('/api/v1/assistants',json={'name':'Unauthorized shared','scope':'team'}).status_code==403

def test_delegate_cannot_change_sharing_delete_or_grant_permissions(client):
    _,_,first,_=setup_grant(client)
    url='/api/v1/assistants/'+first['id']
    for patch in [{'scope':'private'},{'access':{'all_members':False,'user_ids':[]}}]:
        response=client.patch(url,json=patch)
        assert response.status_code==403
        assert response.json()['error']['code']=='ASSISTANT_ACCESS_ADMIN_REQUIRED'
    assert client.delete(url).status_code==403
    assert client.patch(url,json={'editor_user_ids':['attacker']}).status_code==422
    assert client.get(url).json()['scope']=='team'

def test_revocation_is_immediate_and_other_member_never_inherits(client):
    _,_,first,_=setup_grant(client)
    url='/api/v1/assistants/'+first['id']
    login(client,'other-user')
    assert not client.get(url).json()['can_edit']
    assert client.patch(url,json={'instructions':'forbidden'}).status_code==403
    login(client,'editor-user')
    client.app.state.service.db.execute("UPDATE assistants SET editor_user_ids='[]' WHERE id=?",(first['id'],))
    assert not client.get(url).json()['can_edit']
    assert client.patch(url,json={'instructions':'revoked'}).status_code==403

def test_shared_dependencies_remain_admin_managed_and_private_not_shareable(client):
    member,_,first,_=setup_grant(client)
    private=client.post('/api/v1/skills',json={'name':'Private knowledge','instructions':'Only mine'}).json()
    assert client.patch('/api/v1/assistants/'+first['id'],json={'skill_ids':[private['id']]}).json()['error']['code']=='PRIVATE_DEPENDENCY'
    login(client,'admin')
    connection=client.post('/api/v1/connections',json={'name':'Shared','url':'http://localhost/mcp','scope':'team'}).json()
    login(client,'editor-user')
    assert client.patch('/api/v1/connections/'+connection['id'],json={'name':'Changed'}).status_code==403

def test_malformed_grant_fails_closed(client):
    _,_,first,_=setup_grant(client)
    for raw in ['not-json','"editor-user"','[null]','{}']:
        client.app.state.service.db.execute('UPDATE assistants SET editor_user_ids=? WHERE id=?',(raw,first['id']))
        assert client.patch('/api/v1/assistants/'+first['id'],json={'name':'Denied'}).status_code==403
