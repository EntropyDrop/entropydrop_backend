from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4
import json

import pytest
import models
import auth
import creator_payouts as payouts
import paypal_payouts
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
    monkeypatch.setattr(settings, "PAYPAL_PAYOUTS_ENABLED", True)
    monkeypatch.setattr(settings, "PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setattr(settings, "PAYPAL_SECRET", "test-secret")
    monkeypatch.setattr(settings, "PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")
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
    return client.post('/api/creators/me/payouts',json={"amount_cents":amount,"paypal_email":email,"request_key":key or str(uuid4())})


def result(payout, status="SUCCESS", items=True):
    body={"batch_header":{"payout_batch_id":"BATCH123","batch_status":"SUCCESS","sender_batch_header":{"sender_batch_id":f"creator-{payout.id}"}}}
    if items:
        body['items']=[{"payout_item_id":"ITEM123","transaction_id":"TRANSACTION123","transaction_status":status,
                        "payout_item":{"sender_item_id":payout.id,"receiver":payout.paypal_email,"amount":{"currency":"USD","value":f'{payout.amount_cents/100:.2f}'}}}]
    return body


def approve(client, payout_id):
    return client.post(f'/api/creators/manage/payouts/{payout_id}/review',json={"action":"approve"})


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


def test_reservation_idempotency_and_balance_protect_against_double_withdrawal(client, db, people):
    earnings(db); key=str(uuid4())
    first=request(client,600,key); assert first.status_code==200,first.text
    assert request(client,600,key).json()['id']==first.json()['id']
    assert request(client,500,key).status_code==409
    assert request(client,500).status_code==409
    assert client.get('/api/creators/me').json()['balance']['available_cents']==400
    people('creator-b'); assert client.get('/api/creators/me/payouts').json()['total']==0
    assert approve(client,first.json()['id']).status_code==403


def test_admin_rejection_releases_balance_and_is_audited(client, db, people):
    earnings(db); identifier=request(client).json()['id'];people('admin')
    path=f'/api/creators/manage/payouts/{identifier}/review'
    assert client.post(path,json={"action":"reject"}).status_code==400
    response=client.post(path,json={"action":"reject","note":"Please verify your PayPal address"})
    assert response.status_code==200 and response.json()['status']=='rejected'
    assert [event['event'] for event in response.json()['events']]==['requested','rejected']
    people(); assert client.get('/api/creators/me').json()['balance']['available_cents']==1000


def test_approval_sends_once_and_waits_for_item_confirmation(client, db, people, monkeypatch):
    earnings(db); identifier=request(client).json()['id'];row=db.get(models.CreatorPayout,identifier)
    send=Mock(return_value=result(row,items=False)); monkeypatch.setattr(payouts,'create_batch',send)
    people('admin'); response=approve(client,identifier)
    assert response.status_code==200,response.text
    assert response.json()['status']=='processing' and send.call_count==1
    approve(client,identifier); assert send.call_count==1
    monkeypatch.setattr(payouts,'get_batch',lambda batch:result(row))
    assert client.post(f'/api/creators/manage/payouts/{identifier}/review',json={"action":"sync"}).json()['status']=='paid'
    people(); balance=client.get('/api/creators/me').json()['balance']
    assert balance['paid_cents']==1000 and balance['available_cents']==0


def test_refund_before_approval_blocks_transfer_and_late_refund_offsets_future_earnings(client, db, people, monkeypatch):
    order=earnings(db);identifier=request(client).json()['id'];people('admin')
    order.refund_status='pending';db.commit();send=Mock();monkeypatch.setattr(payouts,'create_batch',send)
    assert approve(client,identifier).status_code==409;send.assert_not_called()
    order.refund_status=None;db.commit()
    row=db.get(models.CreatorPayout,identifier);send.return_value=result(row)
    assert approve(client,identifier).json()['status']=='paid'
    order.status='refunded';db.commit();people()
    summary=client.get('/api/creators/me').json()['balance'];assert summary['adjustment_cents']==1000
    earnings(db,amount=1500)
    assert client.get('/api/creators/me').json()['balance']['available_cents']==500


@pytest.mark.parametrize('status,expected,available',[('UNCLAIMED','unclaimed',0),('ONHOLD','on_hold',0),('FAILED','failed',1000),('RETURNED','returned',1000)])
def test_paypal_item_status_controls_balance(client, db, people, monkeypatch,status,expected,available):
    earnings(db); identifier=request(client).json()['id'];row=db.get(models.CreatorPayout,identifier)
    monkeypatch.setattr(payouts,'create_batch',lambda *args:result(row,status));people('admin')
    assert approve(client,identifier).json()['status']==expected
    people();assert client.get('/api/creators/me').json()['balance']['available_cents']==available


def test_timeout_retries_same_identity_and_stops_before_paypal_deduplication_expires(client, db, people, monkeypatch):
    earnings(db);identifier=request(client).json()['id'];people('admin')
    send=Mock(side_effect=TimeoutError());monkeypatch.setattr(payouts,'create_batch',send)
    assert approve(client,identifier).json()['status']=='processing'
    payouts.process_payout(db,identifier,force=True)
    assert len(send.call_args_list)==2 and send.call_args_list[0]==send.call_args_list[1]
    row=db.get(models.CreatorPayout,identifier);row.first_sent_at=datetime.now(timezone.utc)-timedelta(days=30);db.commit()
    payouts.process_payout(db,identifier,force=True);db.refresh(row)
    assert row.status=='manual_review' and send.call_count==2
    people();assert client.get('/api/creators/me').json()['balance']['available_cents']==0


@pytest.mark.parametrize('field',['receiver','amount','sender','batch'])
def test_mismatched_paypal_response_never_marks_paid_or_releases_funds(client,db,people,monkeypatch,field):
    earnings(db);identifier=request(client).json()['id'];row=db.get(models.CreatorPayout,identifier);body=result(row)
    if field=='receiver':body['items'][0]['payout_item']['receiver']='someoneelse@example.test'
    if field=='amount':body['items'][0]['payout_item']['amount']['value']='100.00'
    if field=='sender':body['items'][0]['payout_item']['sender_item_id']='different'
    if field=='batch':body['batch_header']['sender_batch_header']['sender_batch_id']='different'
    monkeypatch.setattr(payouts,'create_batch',lambda *args:body);people('admin')
    assert approve(client,identifier).json()['status']=='manual_review'
    people();assert client.get('/api/creators/me').json()['balance']['available_cents']==0


def test_disabled_payouts_and_active_processing_lease_never_send(client,db,people,monkeypatch):
    earnings(db);identifier=request(client).json()['id'];people('admin')
    send=Mock();monkeypatch.setattr(payouts,'create_batch',send);monkeypatch.setattr(settings,'PAYPAL_PAYOUTS_ENABLED',False)
    assert approve(client,identifier).status_code==409
    monkeypatch.setattr(settings,'PAYPAL_PAYOUTS_ENABLED',True)
    row=db.get(models.CreatorPayout,identifier);row.status='processing';row.processing_until=payouts.now()+timedelta(seconds=60);db.commit()
    payouts.process_payout(db,identifier,force=True);send.assert_not_called()


def test_transport_uses_stable_ids_and_duplicate_lookup_cannot_follow_external_links(monkeypatch):
    monkeypatch.setattr(settings,'PAYPAL_API_BASE','https://api-m.sandbox.paypal.com')
    monkeypatch.setattr(paypal_payouts,'get_paypal_access_token',lambda:'test-token')
    post=Mock(return_value=Mock(ok=True,json=lambda:{'batch_header':{}}));monkeypatch.setattr(paypal_payouts.requests,'post',post)
    paypal_payouts.create_batch('withdrawal',1001,'payout@example.test')
    payload=post.call_args.kwargs
    assert payload['headers']['PayPal-Request-Id']=='creator-withdrawal'
    assert payload['json']['items'][0]['amount']=={'currency':'USD','value':'10.01'}
    lookup=Mock();monkeypatch.setattr(paypal_payouts,'get_batch',lookup)
    post.return_value=Mock(ok=False,status_code=400,json=lambda:{'name':'DUPLICATE_BATCH_ID','links':[{'href':'https://evil.test/v1/payments/payouts/BATCH123'}]})
    with pytest.raises(paypal_payouts.PayoutTransportError):paypal_payouts.create_batch('withdrawal',1001,'payout@example.test')
    lookup.assert_not_called()
    post.return_value.json=lambda:{'name':'DUPLICATE_BATCH_ID','links':[{'href':'https://api-m.sandbox.paypal.com/v1/payments/payouts/BATCH123'}]}
    paypal_payouts.create_batch('withdrawal',1001,'payout@example.test');lookup.assert_called_once_with('BATCH123')


def test_paid_transfer_only_releases_on_confirmed_return(client, db, people, monkeypatch):
    earnings(db);identifier=request(client).json()['id'];row=db.get(models.CreatorPayout,identifier)
    monkeypatch.setattr(payouts,'create_batch',lambda *args:result(row));people('admin');approve(client,identifier)
    for status in ('PENDING','FAILED','UNCLAIMED'):
        monkeypatch.setattr(payouts,'get_batch',lambda batch,status=status:result(row,status))
        payouts.process_payout(db,identifier,force=True);db.refresh(row);assert row.status=='paid'
    wrong=result(row);wrong['items'][0]['payout_item']['amount']['value']='99.00'
    monkeypatch.setattr(payouts,'get_batch',lambda batch:wrong)
    payouts.process_payout(db,identifier,force=True);db.refresh(row);assert row.status=='paid'
    monkeypatch.setattr(payouts,'get_batch',lambda batch:result(row,'RETURNED'))
    payouts.process_payout(db,identifier,force=True);db.refresh(row);assert row.status=='returned'


def test_signed_webhook_schedules_lookup_without_trusting_embedded_status(client, db, people, monkeypatch):
    earnings(db);identifier=request(client).json()['id'];row=db.get(models.CreatorPayout,identifier)
    row.status='processing';row.paypal_batch_id='BATCH123';row.next_check_at=payouts.now()+timedelta(days=1);db.commit()
    monkeypatch.setattr(settings,'PAYPAL_WEBHOOK_ID','test-webhook')
    monkeypatch.setattr('payment_utils.verify_paypal_webhook_signature',lambda *args:True)
    response=client.post('/api/webhooks/paypal',json={'id':'event-1','event_type':'PAYMENT.PAYOUTS-ITEM.SUCCEEDED','resource':{'payout_batch_id':'BATCH123','transaction_status':'SUCCESS'}})
    assert response.status_code==200,response.text
    db.refresh(row);assert row.status=='processing' and payouts.utc(row.next_check_at)<=payouts.now()
    monkeypatch.setattr('payment_utils.verify_paypal_webhook_signature',lambda *args:False)
    assert client.post('/api/webhooks/paypal',json={'event_type':'PAYMENT.PAYOUTS-ITEM.SUCCEEDED'}).status_code==400


def test_payout_migration_preserves_legacy_orders_without_guessing_creator_accounts(monkeypatch):
    import importlib.util
    from pathlib import Path
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    root=Path(__file__).resolve().parents[1]
    def module(name):
        spec=importlib.util.spec_from_file_location(name,root/'alembic/versions'/name)
        loaded=importlib.util.module_from_spec(spec);spec.loader.exec_module(loaded);return loaded
    first=module('f4a92b6d013e_add_creator_referrals.py');second=module('a6d3e9f127c8_creator_accounts_and_payouts.py')
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


def test_legacy_code_binding_restores_history_once_without_self_commission(client, db, people):
    db.add(models.CreatorCode(code='LEGACY',creator_name='Old name',discount_bps=1000,commission_bps=1000))
    order=earnings(db);order.creator_user_id=None;order.creator_code='LEGACY';db.commit()
    people('admin')
    response=client.put('/api/creators/manage/LEGACY',json={'creator_email':'alpha@example.test','discount_bps':1000,'commission_bps':1000,'expected_version':1})
    assert response.status_code==200,response.text
    people();assert client.get('/api/creators/me').json()['balance']['available_cents']==1000
    assert client.get('/api/creators/me/orders').json()['total']==1


def test_rejection_after_uncertain_submission_cannot_release_reserved_money(client, db, people, monkeypatch):
    earnings(db);identifier=request(client).json()['id'];people('admin')
    send=Mock(side_effect=[TimeoutError(), paypal_payouts.PayoutTransportError('paypal_rejected', definitive=True)])
    monkeypatch.setattr(payouts,'create_batch',send)
    assert approve(client,identifier).json()['status']=='processing'
    payouts.process_payout(db,identifier,force=True)
    people();assert client.get('/api/creators/me').json()['balance']['reserved_cents']==1000
    assert db.get(models.CreatorPayout,identifier).status=='processing'


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
    base={'amount_cents':1000,'paypal_email':'valid@example.test','request_key':str(uuid4())}
    for override in ({'amount_cents':True},{'amount_cents':1.2},{'amount_cents':-1}, {'paypal_email':'invalid'}, {'request_key':'wrong'}, {'creator_user_id':'creator-b'}):
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
