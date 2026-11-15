from types import SimpleNamespace
import pytest
from app.harness_attachments import require_image_capability
from app.security import AppError


@pytest.fixture
def prepared_image(tmp_path, monkeypatch):
    """Real SQLite run shape; no POSIX reads, Harness process or model call."""
    import json
    from app.config import Settings
    from app.db import now, uid
    from app.service import Service
    monkeypatch.setenv('AGENT_KNOWLEDGE_EMBEDDING_MODEL', 'local-lexical-v1')
    service=Service(Settings(data_dir=tmp_path,runtime_type='claude',model='test',provider_settings='',model_catalog=''))
    model_id=service.catalog.default_model_id
    service.catalog.models[model_id]['capabilities']=['multimodal']
    user=service.create_user({'username':'image-test','display_name':'Test','password':'image-test-password-123','role':'admin'})
    assistant=service.save_assistant({'name':'Image'},user=user)
    conversation=service.create_conversation({'assistant_id':assistant['id'],'title':'Image'},user)
    file_id=uid()
    row={'id':file_id,'user_id':user['id'],'name':'image.png','mime_type':'image/png','size':12,
         'path':str(tmp_path/'files'/file_id),'created_at':now(),'conversation_id':None}
    service.db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',tuple(row.values()))
    submitted,_=service.prepare_run(conversation['id'],{'text':'Read image','file_ids':[file_id],'client_request_id':uid()},user)
    run=service.require('runs',submitted['run_id'])
    assert 'model_id' not in run  # Runtime receives this row, not run_view().
    assert json.loads(run['execution_meta'])['selection']['model_id']==model_id
    try:
        yield SimpleNamespace(service=service,user=user,conversation=conversation,row=row,run=run,model_id=model_id)
    finally:
        service.db.close()


def recheck_image(fixture, run=None):
    from app.harness_attachments import AttachmentPrompt, _binding
    prompt=AttachmentPrompt.__new__(AttachmentPrompt)
    prompt.service=fixture.service
    prompt.runtime=SimpleNamespace(conversation_id=fixture.conversation['id'])
    prompt.run=run or fixture.run
    prompt.history=[]
    prompt.snapshots=[{'id':fixture.row['id'],'binding':_binding(fixture.row),'sha256':'unused-without-content-read'}]
    prompt.authorize=lambda: fixture.user
    prompt.recheck()


def test_runtime_database_row_uses_frozen_model_selection(prepared_image):
    recheck_image(prepared_image)


def test_frozen_model_cannot_be_replaced_by_public_view_or_current_conversation(prepared_image):
    import json
    fixture=prepared_image
    meta=json.loads(fixture.run['execution_meta'])
    meta['selection']['model_id']='unknown-nonvisual-model'
    run={**fixture.run,'execution_meta':json.dumps(meta),'model_id':fixture.model_id}
    with pytest.raises(AppError) as error:
        recheck_image(fixture,run)
    assert error.value.code=='VISION_UNAVAILABLE'


def test_frozen_visual_model_revocation_still_blocks(prepared_image):
    prepared_image.service.catalog.models[prepared_image.model_id]['capabilities']=[]
    with pytest.raises(AppError) as error:
        recheck_image(prepared_image)
    assert error.value.code=='VISION_UNAVAILABLE'


def test_preflight_checks_selected_model_before_file_io(prepared_image):
    from app.harness_attachments import preflight
    fixture=prepared_image
    with pytest.raises(AppError) as error:
        preflight(fixture.service,fixture.conversation['id'],fixture.user,[fixture.row['id']],model_id='unknown')
    assert error.value.code=='VISION_UNAVAILABLE'
    assert preflight(fixture.service,fixture.conversation['id'],fixture.user,[fixture.row['id']],model_id=fixture.model_id)==[fixture.row]


@pytest.mark.parametrize('metadata', [None, '{bad', '[]', '{"selection":null}', '{"selection":{"model_id":[]}}'])
def test_unrecorded_or_malformed_selection_never_inherits_public_model(prepared_image,metadata):
    with pytest.raises(AppError) as error:
        recheck_image(prepared_image,{**prepared_image.run,'execution_meta':metadata,'model_id':prepared_image.model_id})
    assert error.value.code=='VISION_UNAVAILABLE'


@pytest.mark.parametrize('format,mime', [('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp'),('GIF','image/gif')])
def test_declared_static_rasters_are_actually_decoded(format,mime):
    import io
    from PIL import Image
    from app.harness_attachments import _image
    stream=io.BytesIO()
    Image.new('RGB',(16,12),'green').save(stream,format=format)
    _image(stream.getvalue(),mime)


@pytest.mark.parametrize('case', ['wrong_mime','truncated','animated','dimension'])
def test_invalid_or_animated_images_remain_rejected(case):
    import io
    from PIL import Image
    from app.harness_attachments import _image
    stream=io.BytesIO()
    mime='image/png'
    if case=='animated':
        Image.new('RGB',(10,10),'red').save(stream,format='GIF',save_all=True,append_images=[Image.new('RGB',(10,10),'blue')])
        mime='image/gif'
    else:
        Image.new('RGB',(10001,1) if case=='dimension' else (16,12),'green').save(stream,format='PNG')
    data=stream.getvalue()
    if case=='truncated': data=data[:20]
    if case=='wrong_mime': mime='image/jpeg'
    with pytest.raises(AppError) as error:
        _image(data,mime)
    assert error.value.code==('ATTACHMENT_LIMIT' if case=='dimension' else 'ATTACHMENT_INVALID')

@pytest.mark.parametrize('entry', [{}, {'capabilities':['text']}, {'capabilities':['multimodal'],'enabled':False}])
def test_image_requires_explicit_capability(entry):
    service=SimpleNamespace(catalog=SimpleNamespace(models={'selected':entry}))
    with pytest.raises(AppError) as exc: require_image_capability(service,'selected')
    assert exc.value.code=='VISION_UNAVAILABLE'

def test_image_capability_is_rechecked_after_admin_revocation():
    entry={'capabilities':['multimodal']}
    service=SimpleNamespace(catalog=SimpleNamespace(models={'selected':entry}))
    require_image_capability(service,'selected')
    entry['capabilities']=[]
    with pytest.raises(AppError): require_image_capability(service,'selected')

def test_historical_image_cannot_cross_dispatch_after_revocation(monkeypatch):
    import threading
    from app.harness_attachments import AttachmentPrompt
    service=SimpleNamespace(catalog=SimpleNamespace(models={'selected':{'capabilities':[]}}),db=SimpleNamespace(lock=threading.RLock()))
    prompt=AttachmentPrompt.__new__(AttachmentPrompt)
    prompt.service=service;prompt.runtime=SimpleNamespace(conversation_id='owned')
    prompt.run={'model_id':'selected'};prompt.history=[{'id':'old-image','binding':{}}];prompt.snapshots=[]
    prompt.authorize=lambda: {'id':'owner'}
    monkeypatch.setattr('app.harness_attachments.preflight',lambda *_: [{'name':'old.png'}])
    admitted=[]
    with pytest.raises(AppError) as exc:prompt.dispatch(SimpleNamespace(session_prompt=lambda *args:admitted.append(args)),'native','read')
    assert exc.value.code=='VISION_UNAVAILABLE' and admitted==[]
