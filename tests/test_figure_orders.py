from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
import pytest
import auth
import models
import figure_orders
from main import app


@pytest.fixture
def context(db, monkeypatch):
    admin = models.User(id="figure-admin", email="figure-admin@example.test")
    owner = models.User(id="figure-owner", email="figure-owner@example.test")
    db.add_all([admin, owner])
    cfg = models.ModelSalesLimit(model_type="Cute DIY Kit", order_type="print", stock=298, price=30)
    order = models.Order(id="figure-order", user_id=owner.id, order_type="print", status="paid", price=60, shipping_fee=0,
                         total_price=60, paid_at=datetime.now(timezone.utc), goods_status="awaiting_review", figure_review_status="pending",
                         paypal_order_id="PAYPAL-ORDER", paypal_capture_id="CAPTURE-1", inventory_consumed=True)
    db.add_all([cfg, order])
    db.add_all([models.OrderItem(order_id=order.id, model_type="Cute DIY Kit", price=30, refer_log_id=f"skin-{i}",
                                source_snapshot={"skin_id": f"skin-{i}", "publisher_id": owner.id, "license": "cc-by-nc-4.0"}) for i in range(2)])
    db.commit()
    app.dependency_overrides[auth.get_current_admin] = lambda: admin
    app.dependency_overrides[auth.get_current_user] = lambda: owner
    payload = {"id": "PAYPAL-ORDER", "status": "COMPLETED", "purchase_units": [{"custom_id": order.id,
        "amount": {"value": "60.00", "currency_code": "USD"}, "payments": {"captures": [{"id": "CAPTURE-1", "status": "COMPLETED", "amount": {"value": "60.00", "currency_code": "USD"}}]}}]}
    refund = {"id": "REFUND-1", "status": "COMPLETED", "amount": {"value": "60.00", "currency_code": "USD"},
              "links": [{"rel": "up", "href": "https://api.paypal.com/v2/payments/captures/CAPTURE-1"}]}
    read_order, post, read_refund = Mock(return_value=payload), Mock(return_value=refund), Mock(return_value=refund)
    monkeypatch.setattr(figure_orders, "get_paypal_order_api", read_order)
    monkeypatch.setattr(figure_orders, "refund_paypal_capture_api", post)
    monkeypatch.setattr(figure_orders, "get_paypal_refund_api", read_refund)
    return dict(order=order, admin=admin, owner=owner, cfg=cfg, payload=payload, refund=refund, post=post, read_refund=read_refund)


def review(client, **kwargs):
    return client.post('/api/figure/orders/figure-order/review', json=kwargs)


def test_non_admin_cannot_read_or_mutate(client, db, context, monkeypatch):
    app.dependency_overrides.pop(auth.get_current_admin)
    monkeypatch.setattr(auth.settings, 'ADMIN_EMAILS', 'figure-admin@example.test')
    for method, path, body in [('get', '', None), ('post', '/figure-order/review', {'decision':'approve'}), ('post', '/figure-order/refund/sync', None), ('post','/figure-order/fulfillment',{'stage':'printing'})]:
        result = getattr(client, method)('/api/figure/orders'+path, **({'json':body} if body else {}))
        assert result.status_code == 403
    context['post'].assert_not_called()


def test_production_requires_review_and_valid_sequence(client, db, context):
    path='/api/figure/orders/figure-order/fulfillment'
    for stage in ['printing','shipping','completed']:
        assert client.post(path, json={'stage':stage,'tracking_number':'Carrier 123'}).status_code == 409
    assert review(client, decision='approve').status_code == 200
    assert review(client, decision='approve').status_code == 200
    assert client.post(path, json={'stage':'shipping','tracking_number':'Carrier 123'}).status_code == 409
    for _ in range(2):
        assert client.post(path, json={'stage':'printing'}).status_code == 200
    assert client.post(path, json={'stage':'shipping','tracking_number':'  '}).status_code == 422
    assert client.post(path, json={'stage':'shipping','tracking_number':'Carrier 123'}).status_code == 200
    assert client.post(path, json={'stage':'completed'}).status_code == 200
    assert review(client, decision='reject', reason='late rejection').status_code == 409
    assert db.query(models.ForumNotification).filter_by(type='figure_approved').count() == 1
    assert db.query(models.ForumNotification).filter_by(type='figure_shipped').one().message == 'Carrier 123'
    context['post'].assert_not_called()


def test_review_does_not_gate_production_on_remaining_stock(client, db, context):
    context['cfg'].stock = 0; db.commit()
    assert review(client, decision='approve').status_code == 200
    assert client.post('/api/figure/orders/figure-order/fulfillment',json={'stage':'printing'}).status_code == 200


@pytest.mark.parametrize('status',['pending_payment','cancelled','shipping','completed','refunded'])
def test_only_unreviewed_paid_orders_can_be_reviewed(client, db, context, status):
    context['order'].status=status; db.commit()
    assert review(client, decision='approve').status_code == 409
    assert review(client, decision='reject', reason='no permission').status_code == 409
    context['post'].assert_not_called()


def test_rejection_requires_reason_refunds_full_order_once_and_notifies(client, db, context):
    assert review(client, decision='reject', reason=' \n ').status_code == 422
    result=review(client, decision='reject', reason='Source permission does not cover this order')
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'refunded'
    assert result.json()['figure_reviewed_by'] == context['admin'].id
    assert context['post'].call_args.args[:2] == ('CAPTURE-1','60.00')
    assert review(client, decision='reject', reason='different repeated reason').status_code == 200
    assert client.post('/api/figure/orders/figure-order/refund/sync').status_code == 200
    assert context['post'].call_count == 1
    db.refresh(context['cfg']); assert context['cfg'].stock == 300
    assert db.query(models.ForumNotification).filter_by(type='figure_rejected').count() == 1
    assert db.query(models.ForumNotification).filter_by(type='figure_refunded').count() == 1
    assert review(client, decision='approve').status_code == 409
    assert client.post('/api/figure/orders/figure-order/fulfillment',json={'stage':'printing'}).status_code == 409
    mailbox=client.get('/api/forum/notifications').json()
    assert mailbox['unread_count'] == 2
    assert all(n['orderId']=='figure-order' and 'permission' in n['message'] for n in mailbox['notifications'])
    app.dependency_overrides[auth.get_current_user]=lambda:context['admin']
    assert client.get('/api/forum/notifications').json()['notifications'] == []


def test_timeout_keeps_rejection_and_retries_same_refund_id(client, db, context):
    context['post'].side_effect=TimeoutError()
    result=review(client, decision='reject', reason='Missing rights')
    assert result.status_code==200 and result.json()['status']=='refund_pending'
    assert result.json()['refund_error']
    first_key=context['post'].call_args.args[2]
    context['post'].side_effect=None
    assert client.post('/api/figure/orders/figure-order/refund/sync').json()['status']=='refunded'
    assert context['post'].call_args.args[2]==first_key
    assert db.query(models.ForumNotification).count()==2


def test_pending_refund_is_polled_not_posted_again(client, db, context):
    context['refund']['status']='PENDING'
    assert review(client,decision='reject',reason='Missing rights').json()['status']=='refund_pending'
    context['refund']['status']='COMPLETED'
    assert client.post('/api/figure/orders/figure-order/refund/sync').json()['status']=='refunded'
    assert context['post'].call_count==1 and context['read_refund'].call_count==1


def test_lost_response_adopts_existing_refund(client, db, context):
    context['payload']['purchase_units'][0]['payments']['refunds']=[{'id':'REFUND-1'}]
    assert review(client,decision='reject',reason='Missing rights').json()['status']=='refunded'
    context['post'].assert_not_called()


@pytest.mark.parametrize('corrupt',['binding','capture','partial','currency'])
def test_mismatched_payment_or_refund_is_never_reported_success(client, db, context, corrupt):
    if corrupt=='binding': context['payload']['purchase_units'][0]['custom_id']='other-order'
    if corrupt=='capture': context['payload']['purchase_units'][0]['payments']['captures'][0]['id']='other-capture'
    if corrupt=='partial': context['refund']['amount']['value']='30.00'
    if corrupt=='currency': context['refund']['amount']['currency_code']='EUR'
    result=review(client,decision='reject',reason='Missing rights').json()
    assert result['status']=='refund_pending' and result['refund_status']=='manual_review'
    assert db.query(models.ForumNotification).filter_by(type='figure_refunded').count()==0
    db.refresh(context['cfg']); assert context['cfg'].stock==298


def test_old_unknown_refund_is_sync_only(client, db, context):
    context['post'].side_effect=TimeoutError()
    review(client,decision='reject',reason='Missing rights')
    context['order'].refund_requested_at=datetime.now(timezone.utc)-timedelta(days=2);db.commit()
    result=client.post('/api/figure/orders/figure-order/refund/sync').json()
    assert result['refund_status']=='manual_review'
    assert context['post'].call_count==1


def test_failed_refund_does_not_produce_or_claim_completion(client, db, context):
    context['refund']['status']='FAILED'
    result=review(client,decision='reject',reason='Missing rights').json()
    assert result['status']=='refund_pending' and result['refund_status']=='failed'
    assert db.query(models.ForumNotification).filter_by(type='figure_refund_delayed').count()==1
    assert db.query(models.ForumNotification).filter_by(type='figure_refunded').count()==0


def test_admin_list_is_scoped_and_contains_snapshot_not_internal_error_for_owner(client, db, context):
    db.add(models.Order(user_id=context['owner'].id,order_type='subscription',status='paid')); db.commit()
    result=client.get('/api/figure/orders').json()
    assert result['total']==1 and len(result['items'][0]['items'])==2
    assert result['items'][0]['items'][0]['source_snapshot']['license']=='cc-by-nc-4.0'
    assert client.get('/api/figure/orders?stage=invalid').status_code==422
    assert client.get('/api/figure/orders?page=0').status_code==422
    owner=client.get('/api/orders/figure-order').json()
    assert owner['figure_review_status']=='pending'
    assert 'refund_error' not in owner and 'figure_reviewed_by' not in owner


def test_background_reconciliation_finishes_queued_refund(client, db, context, monkeypatch):
    from contextlib import contextmanager
    import database
    context['post'].side_effect=TimeoutError()
    review(client,decision='reject',reason='Missing rights')
    context['order'].refund_checked_at=datetime.now(timezone.utc)-timedelta(minutes=6)
    db.commit()
    context['post'].side_effect=None
    @contextmanager
    def session():
        yield db
    monkeypatch.setattr(database, 'SessionLocal', session)
    figure_orders.reconcile_figure_refunds()
    db.refresh(context['order'])
    assert context['order'].status=='refunded'
    assert db.query(models.ForumNotification).filter_by(type='figure_refunded').count()==1


def test_paypal_refund_transport_has_fixed_amount_idempotency_and_timeout(monkeypatch):
    import payment_utils
    monkeypatch.setattr(payment_utils,'get_paypal_access_token',lambda:'fixture-token')
    response=Mock(); response.json.return_value={'id':'REFUND-1'}
    post=Mock(return_value=response)
    monkeypatch.setattr(payment_utils.requests,'post',post)
    payment_utils.refund_paypal_capture_api('CAPTURE-1','60.00','stable-request-key')
    assert post.call_args.args[0].endswith('/v2/payments/captures/CAPTURE-1/refund')
    args=post.call_args.kwargs
    assert args['json']=={'amount':{'value':'60.00','currency_code':'USD'}}
    assert args['headers']['PayPal-Request-Id']=='stable-request-key'
    assert args['timeout']==payment_utils.PAYPAL_TIMEOUT_SECONDS


def test_figure_migration_preserves_fulfilled_orders_and_requires_review_for_unstarted():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text, inspect
    migration_path=Path(__file__).parents[1]/'alembic/versions/a4e19c7d8032_add_figure_order_review.py'
    spec=importlib.util.spec_from_file_location('figure_review_migration',migration_path)
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE orders (id TEXT PRIMARY KEY, order_type TEXT, status TEXT, goods_status TEXT)'))
        connection.execute(text('CREATE TABLE order_items (id TEXT PRIMARY KEY)'))
        connection.execute(text('CREATE TABLE forum_notifications (id TEXT PRIMARY KEY)'))
        for id,status,goods in [('unstarted','paid','preparing'),('already-printing','paid','printing'),('shipped','shipping','shipping')]:
            connection.execute(text("INSERT INTO orders VALUES (:id, 'print', :status, :goods)"),dict(id=id,status=status,goods=goods))
        migration.op=Operations(MigrationContext.configure(connection))
        migration.upgrade()
        rows={r.id:r for r in connection.execute(text('SELECT * FROM orders'))}
        assert rows['unstarted'].figure_review_status=='pending' and rows['unstarted'].goods_status=='awaiting_review'
        assert rows['already-printing'].figure_review_status=='approved' and rows['already-printing'].goods_status=='printing'
        assert rows['shipped'].status=='shipping'
        assert all(not r.inventory_consumed for r in rows.values())
        assert 'source_snapshot' in {c['name'] for c in inspect(connection).get_columns('order_items')}
        migration.downgrade()
        assert 'figure_review_status' not in {c['name'] for c in inspect(connection).get_columns('orders')}
