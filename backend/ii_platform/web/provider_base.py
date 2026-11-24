from abc import ABC,abstractmethod
class DataProvider(ABC):
    name="base"
    @abstractmethod
    async def fetch(self,*args,**kwargs): ...
