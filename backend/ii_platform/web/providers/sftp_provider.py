import asyncio
from infrastructure.config.settings import get_settings
class NSESFTPProvider:
    def __init__(self,host=None,port=None,username=None,password=None):
        s=get_settings(); self.host=host or s.sftp_host; self.port=port or s.sftp_port
        self.username=username or s.sftp_username; self.password=password or s.sftp_password
        if not all([self.host,self.username,self.password]): raise RuntimeError("SFTP_HOST, SFTP_USERNAME and SFTP_PASSWORD are required")
    async def fetch(self,remote_path):
        try: import paramiko
        except ImportError as exc: raise RuntimeError("paramiko is required for SFTP ingestion") from exc
        def _fetch():
            transport=paramiko.Transport((self.host,self.port)); transport.connect(username=self.username,password=self.password)
            sftp=paramiko.SFTPClient.from_transport(transport)
            with sftp.open(remote_path,"rb") as f: data=f.read()
            sftp.close(); transport.close(); return data
        return await asyncio.to_thread(_fetch)
