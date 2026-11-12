from contextlib import contextmanager
from time import perf_counter
from datamate_v2.common import utc_now
from datamate_v2.progress import emit

class Profiler:
    def __init__(self,question): self.question=question;self.stages=[]
    @contextmanager
    def stage(self,name,**extra):
        start=perf_counter();record={'timestamp':utc_now(),'question_id':self.question['id'],'business_id':self.question['business_id'],'stage':name,'status':'success','model_call_index':None,'is_repair':False,'error_type':None,**extra}
        emit(name,f'{self.question["id"]}: {name}',question_id=self.question['id'],business_id=self.question['business_id'])
        try: yield
        except Exception as error:
            record.update(status='error',error_type=type(error).__name__);raise
        finally:
            record['duration_ms']=(perf_counter()-start)*1000;self.stages.append(record)
