from bs4 import BeautifulSoup
class FinancialWebCollector:
    async def parse_html(self,html):
        soup=BeautifulSoup(html,"lxml")
        return {"title":soup.title.get_text(strip=True) if soup.title else None,"text":soup.get_text(" ",strip=True)}
