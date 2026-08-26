import csv
import json
from copy import deepcopy

import scraper.update as update
from scraper.audit_unpriced import classify_audited_row, extract_price_leads, source_was_manually_reviewed
from scraper.update import (
    BASELINE,
    CEE_REGISTRY,
    EUROPE_REGISTRY,
    EUROPE_WATCHLIST,
    PROVIDER_MASTER_CROSSCHECK,
    apply_cee_verified_overlay,
    apply_audit_corrections,
    apply_europe_verified_overlay,
    adyen_country_context,
    build_cee_audit,
    calc,
    normalize_revolut_cee_offers,
    normalize_unpriced_pricing_models,
    is_source_reviewed_offer,
    infer_price_verified_on,
    initialise_temporal_metadata,
    parse_adyen_catalog,
    parse_adyen_fee_text,
    parse_cnb,
    parse_fee_candidates,
    resolve_nuxt_payload,
    select_candidate,
    sync_adyen_cee_offers,
    validate_temporal_metadata,
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


def test_default_calculation_uses_twenty_euro_reference_transaction():
    offer={'variable_pct_min':1.0,'variable_pct_max':1.0,'fixed_fee_min':0.05,'fixed_fee_max':0.05,'fee_currency':'EUR'}
    fx={'EUR':{'czk_per_unit':25.0}}
    x=calc(offer,fx)
    assert x['fee_min_czk']==6.25
    assert x['effective_min_pct']==1.25


def test_minimum_fee_floor_is_applied_after_fx_conversion():
    offer={'variable_pct_min':1.0,'variable_pct_max':1.0,'fixed_fee_min':0,'fixed_fee_max':0,'minimum_fee':0.50,'fee_currency':'EUR'}
    fx={'EUR':{'czk_per_unit':25.0}}
    x=calc(offer,fx,500)
    assert x['fee_min_czk']==12.5
    assert x['effective_min_pct']==2.5


def test_minimum_fee_dominates_a_low_value_transaction():
    offer={'variable_pct_min':0.1,'variable_pct_max':0.2,'fixed_fee_min':0,'fixed_fee_max':0,'minimum_fee':1,'fee_currency':'EUR'}
    fx={'EUR':{'czk_per_unit':25.0}}
    x=calc(offer,fx,100)
    assert x['fee_min_czk']==25
    assert x['fee_max_czk']==25
    assert x['effective_min_pct']==25
    assert x['effective_max_pct']==25


def test_icpp_comparison_addon_stays_separate_from_published_variable_rate():
    offer={
        'variable_pct_min':0.69,'variable_pct_max':0.69,'fixed_fee_min':0,'fixed_fee_max':0,
        'fee_currency':'EUR','all_in_complete':False,'comparison_estimate':True,
        'variable_pct_basis':'provider_published',
        'pricing_components':{'comparison_reference':{'total_addon_pct_min':0.278,'total_addon_pct_max':0.303}},
    }
    result=calc(offer,{'EUR':{'czk_per_unit':25}},500)
    assert offer['variable_pct_min']==0.69
    assert result['effective_min_pct']==0.968
    assert result['effective_max_pct']==0.993


def test_package_fee_is_converted_to_effective_rate_at_full_limit_usage():
    offer={
        'variable_pct_min':0,'variable_pct_max':0,'fixed_fee_min':0,'fixed_fee_max':0,
        'monthly_fee':19.9,'package_effective_pct':1.3267,'fee_currency':'EUR',
    }
    fx={'EUR':{'czk_per_unit':25.0}}
    x=calc(offer,fx)
    assert x['effective_min_pct']==1.3267


def test_monthly_fee_without_public_volume_basis_is_not_ranked():
    offer={
        'variable_pct_min':0,'variable_pct_max':0,'fixed_fee_min':0,'fixed_fee_max':0,
        'monthly_fee':5,'fee_currency':'EUR',
    }
    fx={'EUR':{'czk_per_unit':25.0}}
    x=calc(offer,fx)
    assert x['effective_min_pct'] is None


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
    assert (by_id['CZ-barion-fixed-card']['variable_pct_min'],by_id['CZ-barion-fixed-card']['variable_pct_max'])==(0.69,1.49)
    assert (by_id['CZ-barion-fixed-a2a']['variable_pct_min'],by_id['CZ-barion-fixed-a2a']['variable_pct_max'])==(0.29,1.09)
    assert by_id['CZ-barion-icpp-card']['comparison_estimate'] is True
    assert by_id['CZ-barion-icpp-card']['pricing_components']['comparison_reference']['total_addon_pct']==0.35
    assert 'CZ-barion-advanced-low-card' not in by_id
    assert by_id['CZ-kb-smartpay-ecommerce-card']['provider']=='Worldline / KB SmartPay'
    offers=apply_audit_corrections(update.apply_provider_gap_overlay(
        apply_europe_verified_overlay(offers,baseline['countries']),baseline['countries']
    ))
    by_id={item['id']:item for item in offers}
    assert by_id['CZ-revolut-pay-by-bank-a2a']['fixed_fee_min']==6
    assert 'CZ-payu-po-3-měsících-card-restored' not in by_id
    assert by_id['CZ-payu-ecommerce-card']['variable_pct_min']==1
    assert by_id['CZ-payu-ecommerce-card']['fixed_fee_min']==1
    assert by_id['CZ-payu-online-transfer-a2a']['variable_pct_min']==1
    assert (by_id['PL-payu-current-card']['variable_pct_min'],by_id['PL-payu-current-card']['fixed_fee_min'])==(1.1,0.3)
    assert (by_id['PL-payu-current-a2a']['variable_pct_min'],by_id['PL-payu-current-a2a']['fixed_fee_min'])==(1.1,0.3)
    assert by_id['PL-payu-blik-a2a']['fixed_fee_min']==0.32
    assert by_id['HU-payu-online-card']['variable_pct_min'] is None

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


def test_master_crosscheck_keeps_passporting_and_facilitators_out_of_country_registry():
    crosscheck=json.loads(PROVIDER_MASTER_CROSSCHECK.read_text(encoding='utf-8'))
    master=crosscheck['regulatory_master']
    assert len(master['already_represented_or_promoted']) + len(master['new_candidate_groups']) == master['normalised_group_count'] == 63
    candidates={item['provider'] for item in master['new_candidate_groups']}
    assert {'Checkout.com','Stripe','Yapily','BlueSnap','Paynt','Kevin EU'}.issubset(candidates)
    decisions={item['name']:item['decision'] for item in crosscheck['source_assessment']}
    assert decisions['Mastercard registered payment facilitators']=='discovery_only'
    assert decisions['Visa Singapore acquirers']=='excluded_from_europe_crosscheck'


def test_a2a_country_coverage_is_verified_for_reported_gaps():
    registry=json.loads(EUROPE_REGISTRY.read_text(encoding='utf-8'))
    a2a={}
    for item in registry['providers']:
        if 'a2a' in item.get('methods',[]):
            a2a.setdefault(item['country_iso2'],set()).add(item['provider'])
    assert {'GoCardless','TrueLayer','Volt','Trustly'}.issubset(a2a['AT'])
    assert {'Vipps MobilePay','GoCardless','TrueLayer','Volt','Trustly'}.issubset(a2a['DK'])
    assert {'Swish','GoCardless','Volt','Trustly'}.issubset(a2a['SE'])
    assert {'GoCardless','TrueLayer','Volt','Trustly','Banked'}.issubset(a2a['GB'])
    assert {'GoCardless','TrueLayer','Volt'}.issubset(a2a['IE'])
    assert 'TrueLayer' not in a2a['SE']


def test_connectpay_is_added_from_its_official_acquiring_page():
    registry=json.loads(CEE_REGISTRY.read_text(encoding='utf-8'))
    connectpay=next(item for item in registry['providers'] if item['country_iso2']=='LT' and item['provider']=='ConnectPay')
    assert connectpay['role']=='direct_acquirer'
    assert connectpay['official_url']=='https://connectpay.com/card-acquiring/'

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
    assert corrected['pricing_components']['comparison_reference']['scheme_fee_pct']==0.15
    assert corrected['pricing_components']['comparison_reference']['total_addon_pct']==0.35
    assert corrected['comparison_estimate'] is True
    assert discovered['product']=='Bank Button (A2A)'
    assert discovered['fixed_fee_min']==0.11 and discovered['variable_pct_min']==2.0


def test_calc_presents_icpp_as_a_clearly_modelled_debit_reference():
    offer={
        'variable_pct_min':0.6,'variable_pct_max':0.6,
        'fixed_fee_min':0.11,'fixed_fee_max':0.11,'fee_currency':'EUR',
        'pricing_components':{'interchange':{
            'eea_consumer_debit_reference_pct':0.2,
            'eea_consumer_credit_reference_pct':0.3,
        },'comparison_reference':{'total_addon_pct':0.35}},
        'all_in_complete':False,
        'comparison_estimate':True,
    }
    result=update.calc(offer,{'EUR':{'czk_per_unit':25}},amount=500)
    assert result['fee_min_czk']==7.5
    assert result['effective_min_pct']==1.5
    assert result['effective_max_pct']==1.5


def test_swiss_icpp_reference_includes_domestic_cnp_interchange_and_fixed_scheme_fee():
    countries={'CH':{'name':'Švýcarsko'}}
    offers=sync_adyen_cee_offers([],countries,'2026-08-19T00:00:00+00:00')
    offer=next(item for item in offers if item['country_iso2']=='CH' and item['method']=='card')
    reference=offer['pricing_components']['comparison_reference']
    assert offer['variable_pct_min']==0.6
    assert offer['comparison_estimate'] is True
    assert reference['interchange_pct']==0.28
    assert reference['scheme_fee_pct']==0.138
    assert reference['fixed_addon']=={'amount':0.052,'currency':'CHF'}
    result=update.calc(offer,{'EUR':{'czk_per_unit':25},'CHF':{'czk_per_unit':26}},amount=500)
    assert result['fee_min_czk']==9.192
    assert result['effective_min_pct']==1.8384


def test_adyen_card_reference_is_added_outside_cee_with_country_currency():
    countries={'DE':{'name':'Německo'},'GB':{'name':'Spojené království'}}
    offers=sync_adyen_cee_offers([],countries,'2026-08-19T00:00:00+00:00')
    by_country={offer['country_iso2']:offer for offer in offers if offer['method']=='card'}
    assert by_country['DE']['fixed_fee_min']==0.11
    assert by_country['DE']['fee_currency']=='EUR'
    assert by_country['GB']['fixed_fee_min']==0.11
    assert by_country['GB']['fee_currency']=='GBP'
    assert all(offer['comparison_estimate'] is True for offer in by_country.values())


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


def test_provider_role_does_not_change_with_payment_method():
    assert update.provider_role({'provider':'GoPay','provider_type':'PSP s acquiringem','method':'card'})=='psp'
    assert update.provider_role({'provider':'GoPay','provider_type':'PSP s acquiringem','method':'a2a'})=='psp'
    assert update.provider_role({'provider':'Comgate','provider_type':'A2A poskytovatel / acquirer','method':'a2a'})=='acquirer'


def test_provider_role_is_stable_across_card_and_a2a_rows():
    offers=[
        {'country_iso2':'FR','provider':'Mollie','provider_type':'PSP s acquiringem','method':'card'},
        {'country_iso2':'FR','provider':'Mollie','provider_type':'A2A poskytovatel','method':'a2a'},
    ]
    update.assign_provider_roles(offers)
    assert {offer['provider_role'] for offer in offers}=={'psp'}


def test_external_ai_leads_are_added_only_as_verified_acquirers():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    cee_registry=json.loads(CEE_REGISTRY.read_text(encoding='utf-8'))
    europe_registry=json.loads(EUROPE_REGISTRY.read_text(encoding='utf-8'))
    offers=apply_europe_verified_overlay(
        apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries']),
        baseline['countries'],
    )

    cee={(item['country_iso2'],item['provider']):item for item in cee_registry['providers']}
    assert cee[('HU','MBH Bank')]['role']=='acquiring_bank'
    assert cee[('SK','DanubePay')]['role']=='direct_acquirer'
    assert cee[('HR','Global Payments Croatia')]['role']=='direct_acquirer'
    assert cee[('LT','PAYSTRAX')]['role']=='direct_acquirer'

    europe={(item['country_iso2'],item['provider']):item for item in europe_registry['providers']}
    assert europe[('DE','Fiserv / TeleCash')]['role']=='direct_acquirer'
    assert europe[('IT','Numia')]['role']=='direct_acquirer'
    assert europe[('GB','Cashflows')]['role']=='direct_acquirer'
    assert europe[('GR','NBG Pay / IRIS Commerce')]['methods']==['a2a']

    by_provider={(item['country_iso2'],item['provider'],item['method']):item for item in offers}
    assert by_provider[('SK','SKPAY','card')]['variable_pct_min'] is None
    assert by_provider[('SK','SKPAY','card')]['all_in_complete'] is False
    assert by_provider[('GR','NBG Pay / IRIS Commerce','a2a')]['variable_pct_min']==0.6
    assert by_provider[('GR','NBG Pay / IRIS Commerce','a2a')]['cap']==0.5


def test_ccv_netherlands_keeps_debit_and_credit_as_an_honest_range():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_europe_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    ccv=next(offer for offer in offers if offer['id']=='NL-ccv-debit-credit-card')
    assert ccv['variable_pct_min']==0
    assert ccv['variable_pct_max']==1.3
    assert ccv['fixed_fee_min']==0.068
    assert ccv['fixed_fee_max']==0


def test_german_and_portuguese_public_price_corrections_are_kept():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_europe_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    by_id={offer['id']:offer for offer in offers}
    assert by_id['DE-poscash-girocard-card']['variable_pct_min']==0.23
    assert by_id['DE-poscash-girocard-card']['fixed_fee_min']==0
    assert by_id['DE-poscash-girocard-card']['provider']=='POS-cashservice'
    assert by_id['DE-poscash-visa-mastercard-card']['provider']=='POS-cash / secupay'
    assert by_id['DE-fiserv-telecash-spring-card']['variable_pct_min']==0.89
    assert by_id['DE-fiserv-telecash-spring-card']['monthly_fee']==0
    assert by_id['DE-zahlo-eea-consumer-card']['variable_pct_min']==1
    assert by_id['DE-zahlo-eea-consumer-card']['fixed_fee_min']==0.05
    assert by_id['DE-zahlo-qr-a2a']['fixed_fee_min']==0.25
    assert by_id['DE-zahlo-qr-a2a']['monthly_fee']==0
    assert by_id['DE-elavon-debit-card']['variable_pct_min']==0.59
    assert by_id['DE-elavon-credit-card']['fixed_fee_min']==0.01
    assert by_id['DE-vrpayment-card-registry']['package_effective_pct']==1.3267
    assert by_id['DE-payone-visa-mastercard-card']['variable_pct_min']==1.9
    assert by_id['PT-paybyrd-essential-eea-card']['variable_pct_min']==1.25
    assert by_id['PT-reduniqunicre-card-registry']['package_effective_pct']==0.6


def test_cee_public_tariffs_replace_individual_placeholders():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    by_id={offer['id']:offer for offer in offers}
    assert by_id['LT-paysera-local-merchant-acquiring-acceptance-card']['variable_pct_min']==1.45
    assert by_id['LT-paysera-local-merchant-acquiring-acceptance-card']['minimum_fee']==0.15
    assert by_id['PL-tpay-local-merchant-acquiring-acceptance-card']['fixed_fee_min']==0.39
    assert by_id['SK-besteron-local-merchant-acquiring-acceptance-card']['variable_pct_min']==1.4


def test_unpriced_rows_distinguish_quote_only_from_no_public_price():
    offers=[
        {'variable_pct_min':None,'pricing_model':'Individual','verification':'cena na poptávku'},
        {'variable_pct_min':None,'pricing_model':'Individual','verification':'veřejná kompletní sazba nenalezena'},
    ]
    normalized=normalize_unpriced_pricing_models(offers)
    assert normalized[0]['pricing_model']=='Individual'
    assert normalized[1]['pricing_model']=='Not public'


def test_source_reviewed_rule_distinguishes_missing_price_from_missing_review():
    assert is_source_reviewed_offer({'verification':'ověřena lokální nabídka; veřejná sazba nenalezena 19. 8. 2026'})
    assert is_source_reviewed_offer({'verification':'ručně ověřeno v oficiálním ceníku'})
    assert not is_source_reviewed_offer({'verification':'z rozšířeného datasetu'})


def test_price_verification_date_is_separate_from_build_and_tariff_effective_date():
    offer={
        'id':'CZ-example-card',
        'source_status':'manual',
        'source_checked_at':None,
        'verification':'ručně ověřeno v ceníku platném od 2. 6. 2026 dne 25. 8. 2026',
    }
    initialise_temporal_metadata(offer)
    assert offer['source_checked_at'] is None
    assert offer['price_verified_on']=='2026-08-25'


def test_effective_date_alone_is_not_mislabelled_as_price_verification():
    offer={
        'verification':'ručně ověřeno v oficiálním ceníku účinném od 1. 8. 2025',
        'source_status':'manual',
        'source_checked_at':None,
    }
    assert infer_price_verified_on(offer) is None


def test_precise_manual_source_fetch_can_supply_legacy_review_date():
    offer={
        'verification':'ověřeno přímým fetchem primárního zdroje (srpen 2026)',
        'source_status':'manual',
        'source_checked_at':'2026-08-12T19:16:32+00:00',
    }
    assert infer_price_verified_on(offer)=='2026-08-12'


def test_successful_automated_check_supplies_price_review_date():
    offer={
        'verification':'auto-checked (99%)',
        'source_status':'ok',
        'source_checked_at':'2026-08-24T05:27:27+00:00',
    }
    assert infer_price_verified_on(offer)=='2026-08-24'


def test_unreviewed_seed_build_date_does_not_become_price_verification():
    offer={
        'verification':'z rozšířeného datasetu (kontrola datována 22.7.2026 - nejde o živé prověření)',
        'source_status':'seeded',
        'source_checked_at':None,
    }
    assert infer_price_verified_on(offer) is None


def test_previous_verification_date_survives_only_for_unchanged_price_basis():
    previous={
        'source_url':'https://example.com/pricing','product':'Card','variable_pct_min':1.0,
        'price_verified_on':'2026-08-20',
    }
    same={'source_url':'https://example.com/pricing','product':'Card','variable_pct_min':1.0}
    changed={'source_url':'https://example.com/pricing','product':'Card','variable_pct_min':1.1}
    initialise_temporal_metadata(same,previous)
    initialise_temporal_metadata(changed,previous)
    assert same['price_verified_on']=='2026-08-20'
    assert changed['price_verified_on'] is None


def test_temporal_validation_rejects_future_price_review():
    offer={'id':'CZ-future','price_verified_on':'2026-08-26','source_checked_at':None}
    try:
        validate_temporal_metadata([offer],'2026-08-25T09:00:00+00:00')
    except ValueError as exc:
        assert 'Future price_verified_on' in str(exc)
    else:
        raise AssertionError('future price verification date was accepted')


def test_temporal_validation_rejects_build_stamp_on_manual_row():
    offer={
        'id':'CZ-manual','source_status':'manual','price_verified_on':'2026-08-25',
        'source_checked_at':'2026-08-25T09:00:00+00:00',
    }
    try:
        validate_temporal_metadata([offer],'2026-08-25T09:00:00+00:00')
    except ValueError as exc:
        assert 'Build timestamp copied' in str(exc)
    else:
        raise AssertionError('manual row accepted the build timestamp as a source check')


def test_unpriced_audit_only_flags_fee_expressions_in_payment_context():
    text='Online card transaction fee from 1.5% + 0.20 EUR. VAT rate is 21%.'
    leads=extract_price_leads(text)
    assert len(leads)==1
    assert '1.5% + 0.20 EUR' in leads[0]
    assert extract_price_leads('Card transaction volumes increased by 31% during the year.')==[]


def test_blocked_automated_fetch_does_not_erase_manual_source_review():
    assert source_was_manually_reviewed({'verification':'ručně ověřeno v oficiálním FAQ'})
    assert source_was_manually_reviewed({'verification':'ověřena lokální nabídka 20. 8. 2026'})
    assert not source_was_manually_reviewed({'verification':'z rozšířeného datasetu'})
    assert source_was_manually_reviewed({'verification_state':'verified_manual','verification':'legacy text'})
    assert not source_was_manually_reviewed({'verification_state':'legacy_unverified','verification':'ručně ověřeno'})


def test_manual_review_resolves_quote_and_false_positive_price_leads():
    assert classify_audited_row(
        {'pricing_model':'Individual'}, 'public_price_lead_found'
    )=='quote_or_individual_price_confirmed_without_public_number'
    assert classify_audited_row(
        {'pricing_model':'Not public','price_lead_review':'non_merchant_or_incomplete'},
        'public_price_lead_found',
    )=='manual_price_lead_rejected_as_non_merchant_or_incomplete'
    assert classify_audited_row(
        {'pricing_model':'Individual','price_lead_review':'non_merchant_or_incomplete'},
        'public_price_lead_found',
    )=='manual_price_lead_rejected_as_non_merchant_or_incomplete'


def test_clearhaus_domestic_issuer_label_is_not_a_national_scheme():
    offers=[{'provider':'Clearhaus','method':'card','card_scheme':'domestic'}]
    assert update.normalize_card_schemes(offers)[0]['card_scheme']=='intl'


def test_clearhaus_minimum_is_not_treated_as_fixed_addon():
    offers=[{
        'provider':'Clearhaus','method':'card','provider_type':'Veřejný ceník',
        'variable_pct_min':1.45,'fixed_fee_min':0.2,'fixed_fee_max':0.2,
        'minimum_fee':None,
    }]
    normalized=update.normalize_clearhaus_minimum_fees(offers)[0]
    assert normalized['provider_type']=='Acquirer'
    assert normalized['fixed_fee_min']==0
    assert normalized['fixed_fee_max']==0
    assert normalized['minimum_fee']==0.2


def test_country_suffix_is_removed_from_provider_display_name():
    offers=[
        {'provider':'PayU Czech'}, {'provider':'PayU Poland'}, {'provider':'PayU'},
        {'provider':'Flatpay Denmark'}, {'provider':'Elavon UK'},
        {'provider':'Worldline Norway / Bambora'},
    ]
    assert [o['provider'] for o in update.normalize_provider_names(offers)]==[
        'PayU', 'PayU', 'PayU', 'Flatpay', 'Elavon', 'Worldline / Bambora',
    ]


def test_dashboard_names_national_schemes_and_title_resets_all_filters():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    for country, scheme in {
        'BE':'Bancontact',
        'BG':'Bcard',
        'DE':'girocard',
        'DK':'Dankort',
        'FR':'Cartes Bancaires (CB)',
        'IT':'PagoBANCOMAT',
        'PT':'MULTIBANCO',
        'SI':'Karanta',
        'MT':'CashlinkMALTA',
        'CH':'PostFinance Card',
    }.items():
        assert f"{country}:{{name:'{scheme}'" in dashboard
    assert dashboard.count('ecb2024:true')==9
    assert 'class="national-scheme-key"' in dashboard
    assert "nationalScheme=NATIONAL_SCHEMES[S.country]" in dashboard
    assert 'id="homeReset"' in dashboard
    assert 'data-scheme-tooltip=' in dashboard
    assert 'Object.assign(S, DEFAULT_FILTERS)' in dashboard
    assert "setActiveSegment('method-seg', S.method)" in dashboard
    assert "setActiveSegment('role-seg', S.role)" in dashboard
    assert "setActiveSegment('scheme-seg', S.scheme)" in dashboard
    assert "setActiveSegment('region-seg', S.region)" in dashboard


def test_dashboard_keeps_compact_title_sticky_and_moves_csv_export_to_footer():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert 'class="sticky-home"' in dashboard
    assert 'id="stickyHomeReset"' in dashboard
    assert 'data-reset-dashboard' in dashboard
    assert '.sticky-home{position:sticky;top:0' in dashboard
    assert dashboard.index('class="fxnote"') < dashboard.index('id="export"')
    assert 'href="data/changes.json"' not in dashboard
    controls=dashboard[dashboard.index('<section class="controls">'):dashboard.index('</section>',dashboard.index('<section class="controls">'))]
    assert 'id="export"' not in controls


def test_dashboard_does_not_treat_promos_or_monthly_fees_as_zero_total():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert 'o.promo !== true' in dashboard
    assert "(o.monthly_fee || 0) > 0 && packageEffectivePct == null" in dashboard
    assert 'id="monthlyTransactions"' not in dashboard
    assert 'akční ${value}' in dashboard
    assert 'při využití limitu' in dashboard
    assert 'return [...permanent, ...promos, ...withoutVal]' in dashboard


def test_dashboard_uses_structured_verification_pricing_and_audit_fields():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert "['verified_automated','verified_manual'].includes(o.verification_state)" in dashboard
    assert 'id="pricing-seg"' not in dashboard
    assert 'id="auditContent"' in dashboard
    assert 'source_last_attempt_status' in dashboard
    assert '<meta property="og:title"' in dashboard


def test_provider_gap_overlay_adds_reviewed_high_visibility_providers():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=update.apply_provider_gap_overlay([],baseline['countries'])
    providers={offer['provider'] for offer in offers}
    assert {'PayPal','Square','Checkout.com','SIBS'}.issubset(providers)
    assert all(offer.get('price_verified_on')=='2026-08-25' for offer in offers)


def test_dashboard_search_offers_five_fuzzy_keyboard_accessible_suggestions():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert 'id="searchSuggestions"' in dashboard
    assert 'aria-autocomplete="list"' in dashboard
    assert 'function editDistance(a, b)' in dashboard
    assert '.slice(0,5)' in dashboard
    assert "e.key === 'ArrowDown'" in dashboard
    assert "e.key === 'Enter'" in dashboard
    search_handler=dashboard[dashboard.index("document.getElementById('search').oninput"):dashboard.index("document.getElementById('search').onfocus")]
    suggestion_handler=dashboard[dashboard.index('function selectSearchSuggestion'):dashboard.index('function renderSearchSuggestions')]
    assert "S.provider = ''" not in search_handler
    assert "S.provider=''" not in suggestion_handler
    assert "(!S.country || o.country_iso2 === S.country)" in dashboard
    assert "if (S.provider) rows = rows.filter" in dashboard
    assert "if (S.country) rows = rows.filter" in dashboard


def test_dashboard_uses_update_date_and_omits_duplicate_fx_note():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert 'Změny k poslední aktualizaci k dni <span id="auditUpdatedDate">—</span>' in dashboard
    assert "formatIsoDate(String(S.data.generated_at).slice(0,10))" in dashboard
    assert 'Přepočteno dle kurzu ČNB ze dne' not in dashboard
    assert 'id="fxdate2"' not in dashboard
    assert 'Srovnání poplatků za acquiring' in dashboard


def test_barion_ranges_cover_all_public_cee_tariffs_without_first_tier_shortcut():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    by_id={offer['id']:offer for offer in offers}
    for country in ('CZ','SK','PL','HU'):
        fixed=by_id[f'{country}-barion-fixed-card']
        icpp=by_id[f'{country}-barion-icpp-card']
        a2a=by_id[f'{country}-barion-fixed-a2a']
        assert (fixed['variable_pct_min'],fixed['variable_pct_max'])==(0.69,1.49)
        assert (icpp['variable_pct_min'],icpp['variable_pct_max'])==(0.29,1.09)
        assert (a2a['variable_pct_min'],a2a['variable_pct_max'])==(0.29,1.09)
        corrected=apply_audit_corrections(update.apply_provider_gap_overlay(
            apply_europe_verified_overlay(offers,baseline['countries']),baseline['countries']
        ))
        fixed=next(row for row in corrected if row['id']==f'{country}-barion-fixed-card')
        assert '22. 4. 2026' in fixed['verification']
    assert 'HU-barion-fixed-starter-first-tier-card' not in by_id
    assert 'HU-barion-fixed-advanced-first-tier-card' not in by_id


def test_audit_corrections_rename_finby_fix_monthly_minimum_and_gopay_a2a():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    offers=apply_europe_verified_overlay(offers,baseline['countries'])
    offers=update.apply_provider_gap_overlay(offers,baseline['countries'])
    offers=apply_audit_corrections(normalize_revolut_cee_offers(offers))
    by_id={item['id']:item for item in offers}
    trustpay=by_id['SK-trustpay-local-merchant-acquiring-acceptance-card']
    assert trustpay['provider']=='Finby'
    assert trustpay['variable_pct_min']==0.99
    assert trustpay['monthly_fee']==12
    assert trustpay['monthly_fee_mode']=='minimum_commitment'
    assert by_id['SK-gopay-standard-a2a-restored']['variable_pct_min']==2.2
    assert by_id['SK-gopay-standard-a2a-restored']['monthly_fee']==8
    assert by_id['SK-gopay-standard-card-restored']['variable_pct_max']==2.33
    assert by_id['SK-gopay-standard-card-restored']['card_profile']=='consumer'


def test_dashboard_published_fee_shows_fixed_fee_ranges():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert "`${min.toLocaleString('cs-CZ')}–${max.toLocaleString('cs-CZ')}`" in dashboard


def test_dashboard_uses_conservative_ranges_and_clears_hidden_a2a_scheme_filter():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert "S.sortKey === 'rate' ? 'emax' : 'max'" in dashboard
    assert 'o._c.emax < current._c.emax' in dashboard
    assert "if (o.method !== 'card') return true" in dashboard
    assert "if (S.method !== 'card')" in dashboard
    assert "S.scheme = ''" in dashboard
    assert "return `${isApproximate(o) ? '≈ ' : ''}${range}`" in dashboard


def test_dashboard_includes_map_insets_unique_sources_expandable_rows_and_mobile_cards():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    assert 'id="mapInsets"' in dashboard
    assert "const insetCountries=['IS','MT','CY']" in dashboard
    assert 'new Map(rows.filter(o => o.source_url).map(o => [o.provider,o]))' in dashboard
    assert 'class="offer-detail-row"' in dashboard
    assert 'aria-expanded="false"' in dashboard
    assert 'tr.offerrow{display:grid' in dashboard
    assert 'Exportovat aktuální výběr' in dashboard
    assert "low_volume_fee:'Poplatek při nízkém obratu'" in dashboard
    assert "fee.interval === 'monthly' ? ' měsíčně'" in dashboard
    assert "schemeLbl === 'Amex'" in dashboard


def test_dashboard_orders_total_rate_first_and_keeps_scheme_filter_methodologically_valid():
    dashboard=(update.ROOT/'docs'/'index.html').read_text(encoding='utf-8')
    table=dashboard[dashboard.index('body.innerHTML = `',dashboard.index('function renderCompare')):dashboard.index('</table></div>`',dashboard.index('function renderCompare'))]
    assert table.index('Celková sazba') < table.index('Publikovaná sazba')
    assert '<th>Schéma / metoda</th>' in table
    assert "const methodCell=o.method === 'card'" in table
    assert "o.method === 'card' ? 'Karta' : 'A2A převod'" not in table
    assert 'button.disabled=!available' in dashboard
    assert "if (S.scheme && selected?.disabled)" in dashboard
    assert "S.method='card'" in dashboard
    assert "const amount=`${Number(fee.amount).toLocaleString('cs-CZ')} ${fee.currency}${fee.interval === 'monthly' ? ' měsíčně' : ''}`" in dashboard
    assert "fee.kind === 'monthly_service' ? amount" in dashboard
    assert ' Aktuálně:' not in dashboard


def test_provider_type_is_derived_from_structured_role_and_csv_is_country_sorted(tmp_path,monkeypatch):
    rows=[
        {'id':'z','country_iso2':'SK','provider':'B','provider_role':'psp'},
        {'id':'a','country_iso2':'CZ','provider':'A','provider_role':'acquirer'},
    ]
    update.normalise_provider_types(rows)
    assert [row['provider_type'] for row in rows]==['PSP','Acquirer']
    for row in rows:
        row['calculation_reference']={}
    monkeypatch.setattr(update,'DATA',tmp_path)
    update.write_csv({'offers':rows})
    with (tmp_path/'latest.csv').open(encoding='utf-8-sig',newline='') as handle:
        exported=list(csv.DictReader(handle))
    assert [row['country_iso2'] for row in exported]==['CZ','SK']
    assert exported[0]['id']=='a'


def test_audit_corrections_lock_recent_semantic_price_fixes():
    baseline=json.loads(BASELINE.read_text(encoding='utf-8'))
    offers=apply_cee_verified_overlay(deepcopy(baseline['offers']),baseline['countries'])
    offers=apply_europe_verified_overlay(offers,baseline['countries'])
    offers=update.apply_provider_gap_overlay(offers,baseline['countries'])
    offers=apply_audit_corrections(normalize_revolut_cee_offers(offers))
    by_id={item['id']:item for item in offers}

    # BOIPA's public example and UniCredit Romania's 0.70% POS fee are not
    # universal e-commerce merchant rates.
    for offer_id in ('IE-boipa-readymade-card-registry','RO-unicredit-ecommerce-card'):
        assert by_id[offer_id]['pricing_model']=='Individual'
        assert by_id[offer_id]['variable_pct_min'] is None
        assert by_id[offer_id].get('effective_pct_min') is None

    qr=by_id['CZ-globalpayments-qr-platba-uctem-a2a-verified']
    assert (qr['variable_pct_min'],qr['fixed_fee_min'],qr['setup_fee'])==(0.79,0.99,299)

    assert by_id['SE-netsnexisweden-card-registry']['variable_pct_min']==1.0
    assert by_id['SE-netsnexisweden-card-registry']['monthly_fee']==99
    assert by_id['FI-netsnexifinland-card-registry']['variable_pct_min']==1.0
    assert by_id['FI-netsnexifinland-card-registry']['monthly_fee']==20
    assert by_id['DK-netsnexidenmark-card-registry']['variable_pct_min']==1.3
    assert by_id['DK-nets-dankort-card']['variable_pct_min']==0.19
    assert by_id['DK-nets-dankort-card']['variable_pct_max']==0.55

    viva=by_id['RO-viva-online-domestic-consumer-card']
    assert viva['monthly_fee']==25
    assert viva['monthly_fee_mode']=='minimum_commitment'
    assert all(
        item.get('monthly_fee_mode')=='minimum_commitment'
        for item in offers if item['provider']=='Viva.com' and item.get('monthly_fee')
    )

    assert by_id['CZ-csob-start-visa-debit-card']['setup_fee']==1890
    assert by_id['CZ-csob-payment-button-a2a']['setup_fee']==1890
    assert by_id['SK-worldline-online-card']['monthly_fee']==2
    assert by_id['HU-teya-card']['monthly_fee']==8000
    assert by_id['HU-teya-card']['monthly_fee_mode']=='minimum_commitment'
    assert by_id['PT-teyaportugal-card-registry']['monthly_fee']==19.99
    assert by_id['GB-teya-card-registry']['monthly_fee']==29.99


def test_generated_adyen_rows_receive_post_parser_manual_review_metadata():
    ids=(
        'EE-adyen-trustly-a2a','LV-adyen-trustly-a2a','LT-adyen-trustly-a2a',
        'SK-adyen-sepa-direct-debit-a2a','GB-adyen-visa-mastercard-markup-card',
    )
    rows=apply_audit_corrections([{'id':offer_id} for offer_id in ids],generated_only=True)
    by_id={row['id']:row for row in rows}
    assert by_id['EE-adyen-trustly-a2a']['source_url']=='https://www.adyen.com/payment-methods/trustly'
    assert all(row['price_verified_on']=='2026-08-25' for row in rows)
    assert all('ručně ověřeno' in row['verification'] for row in rows)


def test_live_update_fetches_only_sources_referenced_by_current_offers():
    configs={'src_a':{'url':'https://a.test'},'src_orphan':{'url':'https://orphan.test'}}
    assert update.referenced_source_configs([{'source_id':'src_a'},{}],configs)=={
        'src_a':configs['src_a']
    }
    try:
        update.referenced_source_configs([{'source_id':'src_missing'}],configs)
    except ValueError as exc:
        assert 'src_missing' in str(exc)
    else:
        raise AssertionError('missing source configuration was accepted')
