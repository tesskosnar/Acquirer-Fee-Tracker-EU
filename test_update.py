import json
from copy import deepcopy

import scraper.update as update
from scraper.update import (
    BASELINE,
    CEE_REGISTRY,
    EUROPE_REGISTRY,
    EUROPE_WATCHLIST,
    apply_cee_verified_overlay,
    apply_europe_verified_overlay,
    adyen_country_context,
    build_cee_audit,
    calc,
    normalize_revolut_cee_offers,
    parse_adyen_catalog,
    parse_adyen_fee_text,
    parse_cnb,
    parse_fee_candidates,
    resolve_nuxt_payload,
    select_candidate,
    sync_adyen_cee_offers,
)

def test_fee_expression():
    c=parse_fee_candidates('Cena 2,1–4,4 % + 0,58 PLN za transakci')[0]
    assert c['pct_min']==2.1 and c['pct_max']==4.4 and c['fixed_min']==0.58 and c['currency']=='PLN'

def test_cnb_amount_denominator():
    text='21.07.2026 #138\nzemě|měna|množství|kód|kurz\nMaďarsko|forint|100|HUF|6,707\nPolsko|zlotý|1|PLN|5,581\nEMU|euro|1|EUR|24,170\nDánsko|koruna|1|DKK|3,233'
    dt,r=parse_cnb(text)
    assert dt=='2026-07-21' and round(r['HUF']['czk_per_unit'],5)==0.06707

def test_500_czk_calculation():
    offer={'variable_pct_min':1.29,'variable_pct_max':1.29,'fixed_fee_min':0.30,'fixed_fee_max':0.30,'fee_currency':'PLN'}
    fx={'PLN':{'czk_per_unit':5.581}}
    x=calc(offer,fx,500)
    assert round(x['fee_min_czk'],4)==8.1243


def test_minimum_fee_floor_is_applied_after_fx_conversion():
    offer={'variable_pct_min':1.0,'variable_pct_max':1.0,'fixed_fee_min':0,'fixed_fee_max':0,'minimum_fee':0.50,'fee_currency':'EUR'}
    fx={'EUR':{'czk_per_unit':25.0}}
    x=calc(offer,fx,500)
    assert x['fee_min_czk']==12.5
    assert x['effective_min_pct']==2.5


def test_cee_overlay_replaces_wrong_rows_and_adds_local_acquirers():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    by_id={item['id']:item for item in offers}
    assert 'RO-netopia-payments-local-merchant-acquiring-acceptance-card' not in by_id
    assert 'SK-thepay-standard-card-restored' not in by_id
    assert 'EE-makecommerce-e-commerce-standard-card' not in by_id
    assert by_id['CZ-gopay-standard-card-restored']['variable_pct_min']==0.95
    assert by_id['BG-paypercut-eea-consumer-card']['variable_pct_min']==1.29
    assert by_id['BG-paynetics-card-acquiring']['variable_pct_min'] is None


def test_revolut_cee_normalization_keeps_cards_and_a2a_distinct():
    rows=[
        {'country_iso2':'CZ','provider':'Revolut','method':'card','fee_currency':'CZK','cap':5,'cap_currency':'EUR'},
        {'country_iso2':'CZ','provider':'Revolut','method':'a2a','fee_currency':'CZK','cap':5,'cap_currency':'EUR'},
    ]
    card,a2a=normalize_revolut_cee_offers(rows)
    assert card['provider_type']=='Acquirer'
    assert card['product']=='Online EEA spotřebitelské karty'
    assert a2a['provider_type']=='A2A poskytovatel'
    assert a2a['cap'] is None and a2a['cap_currency'] is None


def test_independent_cee_registry_reconciles_every_country_and_paypercut():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    registry=json.loads(CEE_REGISTRY.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    audit=build_cee_audit(registry,offers)
    assert len(audit['countries'])==11
    assert audit['discovered_provider_count']>=72
    bg=next(row for row in audit['countries'] if row['country_iso2']=='BG')
    assert bg['matched_acquirers']>=7
    assert 'Paypercut' not in bg['missing_from_dataset']


def test_czech_review_uses_all_in_rates_and_real_acquiring_entities():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    registry=json.loads(CEE_REGISTRY.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    by_id={item['id']:item for item in offers}

    csob=by_id['CZ-csob-start-visa-debit-card']
    assert (csob['variable_pct_min'],csob['variable_pct_max'])==(0.75,1.04)
    assert csob['fixed_fee_min']==0.5
    assert by_id['CZ-csob-payment-button-a2a']['variable_pct_min']==0.89

    shoptet=by_id['CZ-shoptet-pay-enterprise-card']
    assert (shoptet['variable_pct_min'],shoptet['variable_pct_max'])==(1.19,2.16)
    assert (shoptet['fixed_fee_min'],shoptet['fixed_fee_max'])==(1.9,3.08)
    assert by_id['CZ-shoptet-pay-buttons-a2a']['variable_pct_max']==1.8

    assert by_id['CZ-comgate-profi-card']['variable_pct_min']==0.67
    assert by_id['CZ-comgate-profi-a2a']['variable_pct_min']==0.62
    assert by_id['CZ-kb-smartpay-ecommerce-card']['provider']=='Worldline / KB SmartPay'

    cz_roles={item['provider']:item['role'] for item in registry['providers'] if item['country_iso2']=='CZ'}
    assert cz_roles['Comgate']=='direct_acquirer'
    assert cz_roles['Worldline / KB SmartPay']=='direct_acquirer'
    assert cz_roles['UniCredit Bank Czech Republic and Slovakia']=='acquiring_bank'


def test_europe_watchlist_extends_to_switzerland_and_uk():
    watchlist=json.loads(EUROPE_WATCHLIST.read_text(encoding='utf-8'))
    countries={item['country_iso2'] for item in watchlist['providers']}
    assert {'GB','CH'}.issubset(countries)
    assert len(watchlist['providers'])>=70
    paypoint=next(item for item in watchlist['providers'] if item['provider']=='PayPoint / Handepay')
    assert paypoint['role']=='acquirer_sales_channel'

# --- select_candidate: dřív netestováno, přidáno při reviewu 22.7.2026 ---

def test_select_candidate_matches_near_anchor_with_high_confidence():
    offer={'variable_pct_min':1.29,'fee_currency':'PLN','parser':{'anchor':'card payments','auto_parse':True}}
    text='Our card payments fee is 1,29 % + 0,30 PLN per transaction. Unrelated padding text follows here.'
    cand,conf,why=select_candidate(offer,text)
    assert cand is not None and conf>=0.88
    assert cand['pct_min']==1.29 and cand['currency']=='PLN'

def test_select_candidate_prefers_anchored_fee_over_unrelated_percentage():
    # Stránka obsahuje DPH 21 % i skutečný poplatek 1,05 % - kotva "poplatek" musí
    # skóre navést ke skutečnému poplatku, ne ke vzdálenější sazbě DPH.
    offer={'variable_pct_min':1.0,'fee_currency':'CZK','parser':{'anchor':'poplatek','auto_parse':True}}
    text='DPH sazba je 21 %. Náš poplatek za kartu je 1,05 % + 1 Kč za transakci.'
    cand,conf,why=select_candidate(offer,text)
    assert cand is not None and cand['pct_min']==1.05

def test_select_candidate_no_expression_found():
    offer={'variable_pct_min':1.0,'fee_currency':'CZK','parser':{'auto_parse':True}}
    cand,conf,why=select_candidate(offer,'Tato stránka neobsahuje žádné vyjádření v procentech.')
    assert cand is None and conf==0.0 and why=='no fee expression found'

def test_select_candidate_manual_only_source_skips_parsing():
    offer={'variable_pct_min':1.0,'fee_currency':'CZK','parser':{'auto_parse':False}}
    cand,conf,why=select_candidate(offer,'sazba 5 % kdekoliv na stránce')
    assert cand is None and conf==0.0 and why=='manual-only source'


def test_resolve_nuxt_payload_preserves_reactive_wrapper():
    flat=[{'state':1},[2,3],'Reactive',{'answer':4},0.11]
    assert resolve_nuxt_payload(flat)=={'state':['Reactive',{'answer':0.11}]}


def test_adyen_country_processing_fee_inherits_europe_region():
    global_data={
        'globalDataCurrency':[{'sys':{'id':'eur'},'isoCode':'EUR'}],
        'globalDataRegion':[{
            'sys':{'id':'europe'},'processingFeeAmount':0.11,
            'processingFeeCurrency':{'sys':{'id':'eur'}},
        }],
        'globalDataCountry':[{
            'sys':{'id':'cz-id'},'countryCode':'CZ','region':{'sys':{'id':'europe'}},
            'processingFeeAmount':None,'processingFeeCurrency':None,
        }],
    }
    ids,fees=adyen_country_context(global_data)
    assert ids=={'cz-id':'CZ'}
    assert fees['CZ']=={'amount':0.11,'currency':'EUR'}


def test_parse_adyen_fee_keeps_components_separate():
    card=parse_adyen_fee_text('Interchange+ + 0.60%')
    banking=parse_adyen_fee_text('2.30%')
    trustly=parse_adyen_fee_text('€ 0.50')
    trustly_note=parse_adyen_fee_text('€ 0.50 For gaming, gambling and travel additional rates up 3% can apply')
    ranged=parse_adyen_fee_text('from 1% to 2%')
    assert card['icpp'] is True and card['pct_min']==0.6 and card['fixed']==0
    assert banking['pct_min']==2.3 and banking['currency'] is None
    assert trustly['pct_min']==0 and trustly['fixed']==0.5 and trustly['currency']=='EUR'
    assert trustly_note['pct_min']==0 and trustly_note['fixed']==0.5
    assert ranged['pct_min']==1 and ranged['pct_max']==2


def test_adyen_catalog_classifies_a2a_from_official_type_not_name(monkeypatch):
    global_data={
        'globalDataCurrency':[{'sys':{'id':'eur'},'isoCode':'EUR'}],
        'globalDataRegion':[{
            'sys':{'id':'europe'},'processingFeeAmount':0.11,
            'processingFeeCurrency':{'sys':{'id':'eur'}},
        }],
        'globalDataCountry':[{
            'sys':{'id':'cz-id'},'countryCode':'CZ','region':{'sys':{'id':'europe'}},
        }],
    }
    monkeypatch.setattr(update,'adyen_global_data',lambda _html:global_data)
    monkeypatch.setattr(update,'adyen_pricing_rows',lambda _html:{
        'bank-button':{
            'name':'Bank Button','slug':'bank-button','raw_fee':'2%',
            'fee':parse_adyen_fee_text('2%'),
        },
    })
    payload={'en':{'paymentsMethodsData':[{
        'name':'Bank Button',
        'countryData':[{'country':'cz-id','region':'europe'}],
        'paymentMethodTypeCollection':{'items':[{'name':'Online banking'}]},
    }]}}
    catalog=parse_adyen_catalog('<html></html>',payload)
    assert catalog['methods'][0]['types']=={'Online banking'}
    assert catalog['methods'][0]['countries']=={'CZ'}

    card={
        'id':'CZ-adyen-visa-mastercard-markup-card','country_iso2':'CZ',
        'country':'Česko','provider':'Adyen','method':'card',
        'variable_pct_min':0.6,'variable_pct_max':0.6,
        'fixed_fee_min':0.13,'fixed_fee_max':0.13,'fee_currency':'USD',
    }
    offers=sync_adyen_cee_offers([card],{'CZ':{'name':'Česko'}},'2026-08-18T00:00:00+00:00',catalog,'hash')
    corrected=next(o for o in offers if o['method']=='card')
    discovered=next(o for o in offers if o['method']=='a2a')
    assert corrected['fixed_fee_min']==0.11 and corrected['fee_currency']=='EUR'
    assert corrected['pricing_components']['interchange']['eea_consumer_debit_reference_pct']==0.2
    assert discovered['product']=='Bank Button (A2A)'
    assert discovered['fixed_fee_min']==0.11 and discovered['variable_pct_min']==2.0


def test_calc_does_not_present_incomplete_icpp_components_as_total():
    offer={
        'variable_pct_min':0.6,'variable_pct_max':0.6,
        'fixed_fee_min':0.11,'fixed_fee_max':0.11,'fee_currency':'EUR',
        'pricing_components':{'interchange':{
            'eea_consumer_debit_reference_pct':0.2,
            'eea_consumer_credit_reference_pct':0.3,
        }},
        'all_in_complete':False,
    }
    result=update.calc(offer,{'EUR':{'czk_per_unit':25}},amount=1000)
    assert result['effective_min_pct'] is None
    assert result['effective_max_pct'] is None


def test_europe_registry_overlay_covers_all_discovered_providers_and_new_countries():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    registry=json.loads(EUROPE_REGISTRY.read_text(encoding='utf-8'))
    offers=apply_europe_verified_overlay(
        apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries']),
        baseline['countries'],
    )
    audit=update.build_registry_audit(registry,offers)
    assert len(audit['countries'])==21
    assert audit['discovered_provider_count']>=100
    assert all(not row['missing_from_dataset'] for row in audit['countries'])
    assert {'GB','CH'}.issubset({offer['country_iso2'] for offer in offers})
    numeric=[offer for offer in offers if offer['country_iso2'] in {'GB','CH'} and offer['variable_pct_min'] is not None]
    assert len(numeric)>=8


def test_gateway_and_acquirer_sales_channel_roles_stay_distinct():
    assert update.provider_role({'provider':'Redsys','provider_type':'Brána / procesor (bez acquiringu)','method':'card'})=='gateway_processor'
    assert update.provider_role({'provider':'Tyl by NatWest','provider_type':'Acquirer – distribuční kanál','method':'card'})=='acquirer_sales_channel'


def test_ccv_netherlands_keeps_debit_and_credit_as_an_honest_range():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_europe_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    ccv=next(offer for offer in offers if offer['id']=='NL-ccv-debit-credit-card')
    assert ccv['variable_pct_min']==0
    assert ccv['variable_pct_max']==1.3
    assert ccv['fixed_fee_min']==0.068
    assert ccv['fixed_fee_max']==0


def test_clearhaus_domestic_issuer_label_is_not_a_national_scheme():
    offers=[{'provider':'Clearhaus','method':'card','card_scheme':'domestic'}]
    assert update.normalize_card_schemes(offers)[0]['card_scheme']=='intl'
