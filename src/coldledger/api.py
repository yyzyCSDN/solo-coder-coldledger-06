from datetime import timedelta
from .inventory.fefo import reserve
from .quality.release import may_release
from .transport.connections import choose
from .shipping.air_consolidation import (
    ConsolidationLedger, ManifestDraft, chargeable_weight, validate_draft,
)
class ColdLedgerService:
    def reserve_inventory(self,*args,**kwargs): return reserve(*args,**kwargs)
    def release_decision(self,*args,**kwargs): return may_release(*args,**kwargs)
    def next_connection(self,inbound,candidates,minutes): return choose(inbound,candidates,timedelta(minutes=minutes))
    def air_ledger(self): return ConsolidationLedger()
    def validate_air_draft(self,draft,houses,doc_requirements=()): return validate_draft(draft,houses,doc_requirements)
    def commit_air_draft(self,ledger,draft,houses,doc_requirements=()):
        return ledger.commit(draft,houses,doc_requirements)
    def air_chargeable_weight(self,gross_kg,volume_m3): return chargeable_weight(gross_kg,volume_m3)
    def health(self): return {'service':'coldledger','status':'ok','runtime_dependencies':0}
