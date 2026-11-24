from .parser import extract_xlsx
class DocumentHandler:
    async def extract_tables(self,data:bytes,file_type:str):
        if file_type.lower() in {"xlsx","xlsm"}: return extract_xlsx(data)
        raise ValueError(f"Unsupported document type: {file_type}")
