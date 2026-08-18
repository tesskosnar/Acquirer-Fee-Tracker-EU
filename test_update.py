import scraper.update as update
from scraper.update import (
    adyen_country_context,
    calc,
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
