class PPACLPGCollector:
    async def normalize(self,rows): return [{"source_name":"PPAC","category":"LPG","metric_name":r["metric_name"],"metric_value":float(r["value"]),"unit":r.get("unit")} for r in rows]
