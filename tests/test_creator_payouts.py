from datetime import datetime, timedelta, timezone
from uuid import uuid4
import json

import pytest
import models
import auth
import creator_payouts as payouts
from config import settings
from main import app


@pytest.fixture
def people(client, db, monkeypatch):
    rows = [models.User(id="creator-a", email="alpha@example.test", is_creator=True),
            models.User(id="creator-b", email="beta@example.test", is_creator=True),
            models.User(id="buyer", email="sensitive-buyer@example.test"),
            models.User(id="admin", email="admin@example.test")]
    db.add_all(rows); db.flush()
    db.add_all([models.CreatorCode(code="ALPHA", creator_user_id="creator-a", discount_bps=1000, commission_bps=1000),
                models.CreatorCode(code="BETA", creator_user_id="creator-b", discount_bps=1000, commission_bps=1000)])
    db.commit()
    monkeypatch.setattr(settings, "ADMIN_EMAILS", "admin@example.test")
    def login(user="creator-a"):
        app.dependency_overrides[auth.get_current_user] = lambda: db.get(models.User, user)
    login()
    yield login


def earnings(db, owner="creator-a", amount=1000, status="completed", age=3, refund=None):
    row = models.Order(user_id="buyer", creator_user_id=owner, creator_code="ALPHA" if owner=="creator-a" else "BETA",
        price=100, shipping_fee=10, total_price=100, discount_amount=10, creator_commission=amount/100, creator_commission_cents=amount,
        status=status, paid_at=datetime.now(timezone.utc)-timedelta(days=age), refund_status=refund)
    db.add(row); db.commit(); return row


def request(client, amount=1000, key=None, email="payout@example.test"):
    return client.post('/api/creators/me/payouts',json={"amount_cents":amount,"payout_email":email,"request_key":key or str(uuid4())})


def transfer(client, payout_id, reference="tr_1Abc23", fee=150, note=""):
    return client.post(f'/api/creators/manage/payouts/{payout_id}/transfer',json={"transfer_reference":reference,"fee_cents":fee,"note":note})


def reject(client, payout_id, note="Please verify your Stripe email"):
    return client.post(f'/api/creators/manage/payouts/{payout_id}/reject',json={"note":note})


def test_email_binding_marks_creator_and_owner_cannot_be_reassigned(client, db, people):
    people('admin')
    payload={"creator_email":" SENSITIVE-BUYER@EXAMPLE.TEST ","discount_bps":500,"commission_bps":1000}
    response=client.put('/api/creators/manage/NEW',json=payload)
    assert response.status_code==200,response.text
    assert response.json()['creator_email']=='sensitive-buyer@example.test' and 'creator_name' not in response.json()
    db.refresh(db.get(models.User,'buyer')); assert db.get(models.User,'buyer').is_creator
    assert client.put('/api/creators/manage/NEW',json={**payload,"creator_email":"beta@example.test","expected_version":1}).status_code==409
    assert client.put('/api/creators/manage/MISSING',json={**payload,"creator_email":"missing@example.test"}).status_code==404
    people('buyer'); assert client.get('/api/users/me').json()['is_creator'] is True
    assert client.post('/api/creators/quote',json={"code":"NEW","model_type":"missing"}).status_code!=200


def test_dashboard_and_records_are_scoped_masked_and_paginated(client, db, people):
    own=earnings(db); earnings(db,owner='creator-b',amount=9000)
    db.add(models.UserCreatorReferral(user_id='buyer',original_code='ALPHA',original_at=datetime.now(timezone.utc),current_code='BETA',
        current_clicked_at=datetime.now(timezone.utc),current_expires_at=datetime.now(timezone.utc)));db.commit()
    data=client.get('/api/creators/me').json();assert data['balance']['available_cents']==1000
    orders=client.get('/api/creators/me/orders?page_size=1').json()
    users=client.get('/api/creators/me/users?page_size=1').json()
    assert orders['total']==users['total']==1 and orders['total_pages']==1
    assert own.id not in json.dumps(orders) and 'buyer' not in json.dumps(orders) and 'example.test' not in json.dumps(orders)
    assert orders['items'][0]['customer_reference']==users['items'][0]['reference']
    assert client.get('/api/creators/me/orders?page=2&page_size=1').json()['items']==[]
    assert client.get('/api/creators/me/orders?page_size=101').status_code==422
    assert client.get('/api/creators/manage/payouts').status_code==403
    people('buyer'); assert client.get('/api/creators/me').status_code==403


def test_pending_refunded_and_young_orders_are_not_withdrawable(client, db, people):
    earnings(db,status='paid');earnings(db,age=2);earnings(db,refund='pending');earnings(db,status='refunded')
    summary=client.get('/api/creators/me').json()['balance']
    assert summary['available_cents']==0 and summary['pending_cents']==2000
    assert request(client).status_code==409


def test_legacy_code_binding_restores_history_once_without_self_commission(client, db, people):
    db.add(models.CreatorCode(code='LEGACY',creator_name='Old name',discount_bps=1000,commission_bps=1000))
    order=earnings(db);order.creator_user_id=None;order.creator_code='LEGACY';db.commit()
    people('admin')
    response=client.put('/api/creators/manage/LEGACY',json={'creator_email':'alpha@example.test','discount_bps':1000,'commission_bps':1000,'expected_version':1})
    assert response.status_code==200,response.text
    people();assert client.get('/api/creators/me').json()['balance']['available_cents']==1000
    assert client.get('/api/creators/me/orders').json()['total']==1


def test_self_referrals_and_self_order_discounts_are_blocked(client, db, people):
    from creator_referrals import bind_referral, select_offer, visit_receipt
    from fastapi import HTTPException
    code=db.get(models.CreatorCode,'ALPHA')
    bind_referral(db,'creator-a',visit_receipt(code)['token']);db.commit()
    assert db.get(models.UserCreatorReferral,'creator-a') is None
    with pytest.raises(HTTPException) as error:select_offer(db,'creator-a','ALPHA')
    assert error.value.detail=='creator_self_referral'
    assert select_offer(db,'creator-a',None) is None


def test_invalid_withdrawal_values_never_create_records(client, db, people):
    earnings(db)
    base={'amount_cents':1000,'payout_email':'valid@example.test','request_key':str(uuid4())}
    for override in ({'amount_cents':True},{'amount_cents':1.2},{'amount_cents':-1}, {'payout_email':'invalid'}, {'request_key':'wrong'}, {'creator_user_id':'creator-b'}):
        assert client.post('/api/creators/me/payouts',json={**base,**override}).status_code==422
    assert db.query(models.CreatorPayout).count()==0


@pytest.mark.parametrize('age_seconds,eligible', [(3*86400-1, False), (3*86400, True), (3*86400+1, True)])
def test_three_day_cutoff_matches_balance_records_admin_and_withdrawal(client, db, people, monkeypatch, age_seconds, eligible):
    current=datetime(2026,10,17,12,tzinfo=timezone.utc)
    monkeypatch.setattr(payouts,'now',lambda:current)
    order=earnings(db)
    order.paid_at=current-timedelta(seconds=age_seconds);db.commit()
    summary=client.get('/api/creators/me').json()['balance']
    assert summary['available_cents']==(1000 if eligible else 0)
    assert summary['pending_cents']==(0 if eligible else 1000)
    assert client.get('/api/creators/me/orders').json()['items'][0]['status']==('eligible' if eligible else 'pending')
    people('admin')
    code=next(row for row in client.get('/api/creators/manage').json() if row['code']=='ALPHA')
    assert code['eligible_commission']==(10 if eligible else 0)
    assert code['pending_commission']==(0 if eligible else 10)
    people()
    assert request(client).status_code==(200 if eligible else 409)

def test_reservation_idempotency_and_balance_protect_against_double_withdrawal(client, db, people):
    earnings(db); key=str(uuid4())
    first=request(client,600,key); assert first.status_code==200,first.text
    assert request(client,600,key).json()['id']==first.json()['id']
    assert request(client,500,key).status_code==409
    assert request(client,500).status_code==409
    assert client.get('/api/creators/me').json()['balance']['available_cents']==400
    people('creator-b'); assert client.get('/api/creators/me/payouts').json()['total']==0
    assert transfer(client,first.json()['id']).status_code==403
    assert reject(client,first.json()['id']).status_code==403


def test_request_shows_the_five_day_processing_deadline(client, db, people):
    earnings(db); data=request(client).json()
    assert data['status']=='pending_review' and data['fee_cents'] is None and data['net_cents'] is None
    assert data['payout_email']=='payout@example.test' and 'paypal_email' not in data
    created=datetime.fromisoformat(data['created_at']); due=datetime.fromisoformat(data['due_at'])
    assert due.replace(tzinfo=None)-created.replace(tzinfo=None)==timedelta(days=5)


def test_stripe_receipt_deducts_the_amount_and_takes_the_fee_from_it(client, db, people):
    earnings(db); identifier=request(client,1000).json()['id']; people('admin')
    response=transfer(client,identifier,'tr_1Abc23',150,'Sent from Stripe')
    assert response.status_code==200,response.text
    data=response.json()
    assert data['status']=='paid' and data['amount_cents']==1000 and data['fee_cents']==150 and data['net_cents']==850
    assert data['transfer_reference']=='tr_1Abc23' and data['paid_at'] and data['review_note']=='Sent from Stripe'
    assert [event['event'] for event in data['events']]==['requested','paid']
    people(); balance=client.get('/api/creators/me').json()['balance']
    assert balance['paid_cents']==1000 and balance['reserved_cents']==0 and balance['available_cents']==0
    assert client.get('/api/creators/me/payouts').json()['items'][0]['net_cents']==850


def test_receipt_is_idempotent_and_cannot_be_changed_or_reused(client, db, people):
    earnings(db,amount=3000); first=request(client,1000).json()['id']; second=request(client,1000).json()['id']; people('admin')
    assert transfer(client,first).status_code==200
    assert transfer(client,first).status_code==200
    assert transfer(client,first,'tr_other').status_code==409
    assert transfer(client,first,fee=100).status_code==409
    assert transfer(client,second,'tr_1Abc23').status_code==409
    assert db.get(models.CreatorPayout,second).status=='pending_review'
    assert transfer(client,second,'tr_2Def45').status_code==200
    assert db.query(models.CreatorPayout).filter_by(status='paid').count()==2


@pytest.mark.parametrize('reference,fee,status',[('tr_valid',1000,422),('tr_valid',1001,422),('tr_valid',-1,422),('tr valid',10,422),('ab',10,422),('tr_valid',1.5,422)])
def test_invalid_receipts_never_pay_out(client, db, people, reference, fee, status):
    earnings(db); identifier=request(client,1000).json()['id']; people('admin')
    assert transfer(client,identifier,reference,fee).status_code==status
    assert db.get(models.CreatorPayout,identifier).status=='pending_review'
    people(); assert client.get('/api/creators/me').json()['balance']['reserved_cents']==1000


def test_admin_rejection_releases_balance_and_is_audited(client, db, people):
    earnings(db); identifier=request(client).json()['id'];people('admin')
    assert client.post(f'/api/creators/manage/payouts/{identifier}/reject',json={}).status_code==400
    response=reject(client,identifier)
    assert response.status_code==200 and response.json()['status']=='rejected'
    assert [event['event'] for event in response.json()['events']]==['requested','rejected']
    assert reject(client,identifier).status_code==200
    assert transfer(client,identifier).status_code==409
    people(); assert client.get('/api/creators/me').json()['balance']['available_cents']==1000


def test_paid_withdrawals_cannot_be_rejected(client, db, people):
    earnings(db); identifier=request(client).json()['id'];people('admin')
    assert transfer(client,identifier).status_code==200
    assert reject(client,identifier).status_code==409


def test_refund_before_receipt_blocks_payment_and_late_refund_offsets_future_earnings(client, db, people):
    order=earnings(db);identifier=request(client).json()['id'];people('admin')
    order.refund_status='pending';db.commit()
    assert transfer(client,identifier).status_code==409
    assert db.get(models.CreatorPayout,identifier).status=='pending_review'
    order.refund_status=None;db.commit()
    assert transfer(client,identifier).json()['status']=='paid'
    order.status='refunded';db.commit();people()
    summary=client.get('/api/creators/me').json()['balance'];assert summary['adjustment_cents']==1000
    earnings(db,amount=1500)
    assert client.get('/api/creators/me').json()['balance']['available_cents']==500


@pytest.mark.parametrize('legacy',['processing','unclaimed','on_hold','manual_review'])
def test_payouts_left_over_from_paypal_stay_reserved_until_an_admin_resolves_them(client, db, people, legacy):
    earnings(db); identifier=request(client).json()['id']
    row=db.get(models.CreatorPayout,identifier);row.status=legacy;db.commit()
    assert client.get('/api/creators/me').json()['balance']['reserved_cents']==1000
    people('admin'); assert transfer(client,identifier).json()['status']=='paid'
    row.status=legacy;row.transfer_reference=None;row.fee_cents=None;db.commit()
    assert reject(client,identifier).json()['status']=='rejected'


def test_paypal_payout_webhooks_are_acknowledged_and_ignored(client, db, people, monkeypatch):
    earnings(db);identifier=request(client).json()['id'];row=db.get(models.CreatorPayout,identifier)
    monkeypatch.setattr(settings,'PAYPAL_WEBHOOK_ID','test-webhook')
    monkeypatch.setattr('payment_utils.verify_paypal_webhook_signature',lambda *args:True)
    response=client.post('/api/webhooks/paypal',json={'id':'event-1','event_type':'PAYMENT.PAYOUTS-ITEM.SUCCEEDED','resource':{'payout_batch_id':'BATCH123'}})
    assert response.status_code==200,response.text
    db.refresh(row);assert row.status=='pending_review'


def _chain(monkeypatch):
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    root=Path(__file__).resolve().parents[1]
    def module(name):
        spec=importlib.util.spec_from_file_location(name,root/'alembic/versions'/name)
        loaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(loaded);return loaded
    names=('f4a92b6d013e_add_creator_referrals.py','a6d3e9f127c8_creator_accounts_and_payouts.py','d7a2f93e5b14_manual_creator_payouts_with_stripe_receipts.py')
    return sa,Operations,MigrationContext,[module(name) for name in names]


def test_payout_migration_preserves_legacy_orders_without_guessing_creator_accounts(monkeypatch):
    sa,Operations,MigrationContext,(first,second,_)=_chain(monkeypatch)
    engine=sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id VARCHAR(16) PRIMARY KEY)'))
        conn.execute(sa.text('CREATE TABLE orders (id VARCHAR(16) PRIMARY KEY, total_price FLOAT)'))
        conn.execute(sa.text("INSERT INTO orders VALUES ('old-order', 37)"))
        operations=Operations(MigrationContext.configure(conn))
        monkeypatch.setattr(first,'op',operations);monkeypatch.setattr(second,'op',operations)
        first.upgrade();conn.execute(sa.text('UPDATE orders SET creator_commission=4.05'));second.upgrade()
        assert tuple(conn.execute(sa.text('SELECT total_price,creator_commission_cents,creator_user_id FROM orders')).one())==(37,405,None)
        assert 'creator_payouts' in sa.inspect(conn).get_table_names()
        second.downgrade()
        assert 'creator_payouts' not in sa.inspect(conn).get_table_names()
    engine.dispose()


def test_manual_payout_migration_renames_the_email_and_backfills_paid_paypal_rows(monkeypatch):
    sa,Operations,MigrationContext,(first,second,manual)=_chain(monkeypatch)
    engine=sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id VARCHAR(16) PRIMARY KEY)'))
        conn.execute(sa.text('CREATE TABLE orders (id VARCHAR(16) PRIMARY KEY, total_price FLOAT)'))
        operations=Operations(MigrationContext.configure(conn))
        for module in (first,second,manual): monkeypatch.setattr(module,'op',operations)
        first.upgrade();second.upgrade()
        conn.execute(sa.text("INSERT INTO users (id) VALUES ('c')"))
        for identifier,status,txn in (('p1','paid','PAYPALTXN1'),('p2','pending_review',None)):
            conn.execute(sa.text("INSERT INTO creator_payouts (id,creator_user_id,request_key,amount_cents,currency,paypal_email,status,paypal_transaction_id,attempt_count) "
                                 "VALUES (:i,'c',:i,1000,'USD','old@example.test',:s,:t,0)"),{'i':identifier,'s':status,'t':txn})
        manual.upgrade()
        columns={column['name'] for column in sa.inspect(conn).get_columns('creator_payouts')}
        assert {'payout_email','fee_cents','transfer_reference','paid_at'}<=columns and 'paypal_email' not in columns
        rows={row[0]:tuple(row[1:]) for row in conn.execute(sa.text('SELECT id,payout_email,fee_cents,transfer_reference FROM creator_payouts'))}
        assert rows=={'p1':('old@example.test',0,'PAYPALTXN1'),'p2':('old@example.test',None,None)}
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("UPDATE creator_payouts SET fee_cents = 1000 WHERE id = 'p2'"))
    engine.dispose()
    engine=sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id VARCHAR(16) PRIMARY KEY)'))
        conn.execute(sa.text('CREATE TABLE orders (id VARCHAR(16) PRIMARY KEY, total_price FLOAT)'))
        operations=Operations(MigrationContext.configure(conn))
        for module in (first,second,manual): monkeypatch.setattr(module,'op',operations)
        first.upgrade();second.upgrade();manual.upgrade();manual.downgrade()
        columns={column['name'] for column in sa.inspect(conn).get_columns('creator_payouts')}
        assert 'paypal_email' in columns and not {'payout_email','fee_cents','transfer_reference','paid_at'}&columns
    engine.dispose()
