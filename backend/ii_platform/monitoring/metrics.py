try:
    from prometheus_client import Counter, Histogram
except ImportError:
    class _Metric:
        def labels(self,*args,**kwargs): return self
        def inc(self,*args,**kwargs): pass
        def observe(self,*args,**kwargs): pass
    Counter=Histogram=lambda *args,**kwargs:_Metric()
REQUESTS=Counter("investment_api_requests_total","Total API requests")
REQUEST_LATENCY=Histogram("investment_api_request_latency_seconds","API request latency")
EVENT_COUNTER=Counter("investment_events_total","Events published/processed",["event_type","status"])
