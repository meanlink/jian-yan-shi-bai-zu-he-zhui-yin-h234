import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const sequential={
 registry:()=>api('/sequential/registry'),
 get:id=>api('/sequential/'+id),
 register:data=>api('/sequential',{method:'POST',body:JSON.stringify(data)}),
 ingest:(id,data)=>api('/sequential/'+id+'/batches',{method:'POST',body:JSON.stringify(data)}),
 correct:(id,data)=>api('/sequential/'+id+'/corrections',{method:'POST',body:JSON.stringify(data)}),
 reevaluate:(id,data)=>api('/sequential/'+id+'/reevaluate',{method:'POST',body:JSON.stringify(data)}),
 close:id=>api('/sequential/'+id+'/close',{method:'POST'}),
 stop:id=>api('/sequential/'+id+'/stop')
};
