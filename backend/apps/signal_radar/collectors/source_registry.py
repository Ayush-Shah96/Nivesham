"""Curated Signal Radar source registry.

Public government portals are enabled for conservative discovery. Paid data
providers and portals requiring authenticated/captcha workflows are registered
but disabled; the collector never attempts to bypass access controls.
"""

SOURCES = [
    {"id":"parivesh","name":"PARIVESH","category":"CapEx & Expansion","url":"https://parivesh.nic.in/","enabled":True,"access":"public_portal","keywords":["environmental clearance","proposal","project","public hearing","expansion","capacity","industry","manufacturing"]},
    {"id":"mca","name":"Ministry of Corporate Affairs","category":"CapEx & Expansion","url":"https://www.mca.gov.in/","enabled":True,"access":"public_limited","keywords":["charge","company master","annual return","subsidiary","filing","incorporation"]},
    {"id":"midc","name":"MIDC","category":"Factories & Projects","url":"https://www.midcindia.org/","enabled":True,"access":"public_portal","keywords":["land allotment","industrial area","industrial park","plot","investment","project"]},
    {"id":"gidc","name":"GIDC","category":"Factories & Projects","url":"https://gidc.gujarat.gov.in/","enabled":True,"access":"public_portal","keywords":["plot allotment","industrial estate","industrial park","land","project","investment"]},
    {"id":"sipcot","name":"SIPCOT","category":"Factories & Projects","url":"https://sipcot.tn.gov.in/","enabled":True,"access":"public_portal","keywords":["industrial park","land allotment","project","investment","manufacturing","allotment"]},
    {"id":"cpcb","name":"Central Pollution Control Board / SPCB directory","category":"Factories & Projects","url":"https://cpcb.nic.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","environmental clearance","industry","industrial"]},
    {"id":"mpcb","name":"Maharashtra Pollution Control Board","category":"Factories & Projects","url":"https://mpcb.gov.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","industry","environmental clearance","public hearing"]},
    {"id":"gpcb","name":"Gujarat Pollution Control Board","category":"Factories & Projects","url":"https://gpcb.gujarat.gov.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","industry","environmental clearance"]},
    {"id":"tnpcb","name":"Tamil Nadu Pollution Control Board","category":"Factories & Projects","url":"https://tnpcb.gov.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","industry","environmental clearance"]},
    {"id":"kspcb","name":"Karnataka State Pollution Control Board","category":"Factories & Projects","url":"https://kspcb.karnataka.gov.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","industry","environmental clearance"]},
    {"id":"tspcb","name":"Telangana Pollution Control Board","category":"Factories & Projects","url":"https://tspcb.cgg.gov.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","industry","environmental clearance"]},
    {"id":"appcb","name":"Andhra Pradesh Pollution Control Board","category":"Factories & Projects","url":"https://pcb.ap.gov.in/","enabled":True,"access":"public_portal","keywords":["consent to establish","consent to operate","industry","environmental clearance"]},
    {"id":"cppp","name":"Central Public Procurement Portal","category":"Tenders & Contracts","url":"https://eprocure.gov.in/eprocure/app","enabled":True,"access":"public_portal","keywords":["tender","bid","award","procurement","contract","corrigendum"]},
    {"id":"gem","name":"Government e-Marketplace","category":"Tenders & Contracts","url":"https://gem.gov.in/","enabled":True,"access":"public_portal","keywords":["bid","tender","product","seller","order","procurement"]},
    {"id":"ipindia","name":"IP India","category":"Products & Technology","url":"https://ipindia.gov.in/","enabled":True,"access":"public_portal","keywords":["patent","trademark","design","application","journal"]},
    {"id":"wipo","name":"WIPO PATENTSCOPE","category":"Products & Technology","url":"https://patentscope.wipo.int/","enabled":True,"access":"public_portal","keywords":["patent","application","publication","technology"]},
    {"id":"bis","name":"Bureau of Indian Standards","category":"Products & Technology","url":"https://www.bis.gov.in/","enabled":True,"access":"public_portal","keywords":["certification","standard","product","license","testing"]},
    {"id":"cdsco","name":"CDSCO","category":"Products & Technology","url":"https://cdsco.gov.in/","enabled":True,"access":"public_portal","keywords":["drug approval","medical device","clinical trial","permission","approval"]},
    {"id":"egazette","name":"e-Gazette of India","category":"Policy & Regulation","url":"https://egazette.gov.in/","enabled":True,"access":"public_portal","keywords":["notification","draft","amendment","order","rules","restriction"]},
    {"id":"dgft","name":"Directorate General of Foreign Trade","category":"Policy & Regulation","url":"https://www.dgft.gov.in/","enabled":True,"access":"public_portal","keywords":["notification","import","export","restriction","incentive","trade"]},
    {"id":"dpiit","name":"DPIIT","category":"Policy & Regulation","url":"https://dpiit.gov.in/","enabled":True,"access":"public_portal","keywords":["PLI","investment","industrial policy","incentive","manufacturing","FDI"]},
    {"id":"nse","name":"NSE Corporate Announcements","category":"Competitor Activity","url":"https://www.nseindia.com/companies-listing/corporate-filings-announcements","enabled":True,"access":"public_portal","keywords":["capex","acquisition","order","contract","project","expansion","investor presentation","board"]},
    {"id":"bse","name":"BSE Corporate Announcements","category":"Competitor Activity","url":"https://www.bseindia.com/corporates/ann.html","enabled":True,"access":"public_portal","keywords":["announcement","capex","acquisition","order","contract","project","presentation"]},
    {"id":"sec_edgar","name":"SEC EDGAR","category":"Competitor Activity","url":"https://www.sec.gov/edgar/search/","enabled":True,"access":"public_api_preferred","keywords":["8-K","10-K","10-Q","acquisition","capital expenditure","facility","capacity"]},
    {"id":"commerce_trade","name":"Department of Commerce Trade Statistics","category":"Supply Chain & Customers","url":"https://tradestat.commerce.gov.in/","enabled":True,"access":"public_portal","keywords":["export","import","trade statistics","commodity","product","destination"]},
    {"id":"panjiva","name":"Panjiva","category":"Supply Chain & Customers","url":"https://panjiva.com/","enabled":False,"access":"commercial_subscription","keywords":["shipment","supplier","importer","exporter"]},
    {"id":"importgenius","name":"ImportGenius","category":"Supply Chain & Customers","url":"https://www.importgenius.com/","enabled":False,"access":"commercial_subscription","keywords":["shipment","supplier","importer","exporter"]},
    {"id":"volza","name":"Volza","category":"Supply Chain & Customers","url":"https://www.volza.com/","enabled":False,"access":"commercial_subscription","keywords":["shipment","supplier","importer","exporter"]},
]


def public_sources():
    return [s for s in SOURCES if s["enabled"]]
