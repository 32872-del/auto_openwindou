"""US/CA 离线地区规范化；邮编与州省相互核对，不以同名城市猜国家。"""
import re
import unicodedata

# 标准州/省表移植自旧版 core/proxy_manager.py；与网络查询无关。
US=dict(zip('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC'.split(),
 'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia'.split('|')))
CA=dict(zip('AB BC MB NB NL NS NT NU ON PE QC SK YT'.split(),
 'Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland and Labrador|Nova Scotia|Northwest Territories|Nunavut|Ontario|Prince Edward Island|Quebec|Saskatchewan|Yukon'.split('|')))
LABELS={'us':'美国','ca':'加拿大'}

def folded(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value.casefold()) if not unicodedata.combining(c)).strip()

def normalize(raw, *, strict=True):
    result={k:str(raw.get(k,'') or '').strip() for k in ('country','state','city','zip')}
    explicit=result['country'].lower();postal=result['zip'];region=folded(result['state'])
    inferred='';reason=[];code='';region_country=''
    if postal:
        if re.fullmatch(r'[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d',postal):
            inferred='ca';compact=postal.replace(' ','').upper();result['zip']=compact[:3]+' '+compact[3:]
        elif re.fullmatch(r'\d{4,5}(?:-\d{4})?',postal):inferred='us'
        if inferred:reason.append('邮编')
    for country,regions in (('us',US),('ca',CA)):
        for key,name in regions.items():
            if region and region in (folded(key),folded(name)):
                code=key;region_country=country;break
    if region_country:reason.append('州/省')
    evidence={v for v in (inferred,region_country,explicit) if v}
    if len(evidence)>1 and strict:
        raise ValueError('国家信息冲突：邮编 '+(postal or '未填')+'、州/省 '+(result['state'] or '未填')+' 与国家 '+(explicit or '自动识别')+' 不一致，请核对')
    country=explicit or inferred or region_country
    result['country']=country
    if country=='us' and re.fullmatch(r'\d{4}',postal):result['zip']='0'+postal
    if code and country==region_country:result['state']=code
    regions=US if country=='us' else CA if country=='ca' else {}
    result.update(state_code=code or result['state'],state_name=regions.get(code,result['state']),
                  country_label=LABELS.get(country,country),basis='指定国家' if explicit else '、'.join(reason),
                  warning='' if country or not any(result[k] for k in ('state','city','zip')) else '无法确定国家，请补州/省、邮编或国家；同名城市不自动猜测')
    return result

def tokens(raw,style='raw'):
    loc=normalize(raw)
    values={k:loc[k] for k in ('country','state','city','zip')}
    values.update(countryCode=loc['country'],countryUpper=loc['country'].upper(),stateCode=loc['state_code'],stateName=loc['state_name'],citySlug=re.sub(r'\s+','',loc['city']).lower())
    if style=='bolt':values.update(state=loc['state_code'].lower(),city=values['citySlug'])
    elif style=='nine':values.update(state=loc['state_name'],city=loc['city'].title())
    elif style=='iproyal':
        values['city'] = re.sub(r'[^a-z0-9]', '', folded(loc['city']))
    values.update(proxyCountry=loc['country'],proxyState=loc['state_name'],proxyStateCode=loc['state_code'].lower(),proxyCity=values['city'],proxyZip=loc['zip'])
    if style=='iproyal':
        values['proxyState'] = re.sub(r'[^a-z0-9]', '', folded(loc['state_name']))
    return values
