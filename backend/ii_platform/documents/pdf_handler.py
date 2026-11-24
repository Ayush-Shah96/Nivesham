from .parser import extract_pdf_text
class PDFHandler:
    async def extract_text(self,data:bytes)->str: return extract_pdf_text(data)
