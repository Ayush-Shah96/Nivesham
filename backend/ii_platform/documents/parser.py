from pypdf import PdfReader
from openpyxl import load_workbook
from io import BytesIO

def extract_pdf_text(data:bytes)->str:
    reader=PdfReader(BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)

def extract_xlsx(data:bytes)->list[list]:
    wb=load_workbook(BytesIO(data),read_only=True,data_only=True)
    ws=wb.active
    return [list(row) for row in ws.iter_rows(values_only=True)]
