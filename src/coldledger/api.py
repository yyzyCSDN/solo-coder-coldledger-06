from datetime import timedelta
from .inventory.fefo import reserve
from .quality.release import may_release
from .transport.connections import choose
from .airfreight.book import AirCargoBook
from .airfreight.weight import Weight, volumetric_weight
class ColdLedgerService:
    def reserve_inventory(self,*args,**kwargs): return reserve(*args,**kwargs)
    def release_decision(self,*args,**kwargs): return may_release(*args,**kwargs)
    def next_connection(self,inbound,candidates,minutes): return choose(inbound,candidates,timedelta(minutes=minutes))
    def air_cargo_book(self): return AirCargoBook()
    def air_chargeable_weight(self,gross_kg,volume_cm3,pieces=0):
        return Weight.measure(gross_kg,volume_cm3,pieces)
    def air_volumetric_weight(self,volume_cm3): return volumetric_weight(volume_cm3)
    def health(self): return {'service':'coldledger','status':'ok','runtime_dependencies':0}
