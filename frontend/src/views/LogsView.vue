<template>
  <n-space vertical size="large">
    <n-space justify="space-between" align="center">
      <n-h1>调用日志</n-h1>
      <n-button :loading="loading" @click="refresh">刷新</n-button>
    </n-space>
    <n-alert type="info">默认不保存完整 Prompt/Response；可在“系统设置 → 日志隐私”中开启。主调用与每个辅助步骤分别记录，并通过父请求 ID 关联。</n-alert>
    <div data-testid="log-card-stack" class="log-card-stack">
      <n-card title="筛选">
        <div class="filter-grid">
          <n-date-picker v-model:value="filters.range" type="datetimerange" clearable/>
          <n-select v-model:value="filters.inbound_protocol" clearable :options="protocolOptions" placeholder="协议"/>
          <n-select v-model:value="filters.unified_model" clearable filterable :options="unifiedOptions" placeholder="统一模型"/>
          <n-select v-model:value="filters.provider_instance_id" clearable :options="providerOptions" placeholder="供应商"/>
          <n-select v-model:value="filters.upstream_model_id" clearable filterable :options="upstreamOptions" placeholder="上游模型"/>
          <n-select v-model:value="filters.success" clearable :options="statusOptions" placeholder="状态"/>
          <n-select data-testid="log-kind-filter" v-model:value="filters.request_kind" clearable :options="kindOptions" placeholder="调用类型"/>
          <div class="cost-range">
            <n-input-number v-model:value="filters.min_cost" clearable :min="0" placeholder="最低成本"/>
            <span>—</span>
            <n-input-number v-model:value="filters.max_cost" clearable :min="0" placeholder="最高成本"/>
          </div>
        </div>
      </n-card>
      <n-card title="日志结果">
        <div data-testid="log-table-controls" class="result-controls">
          <n-space>
            <n-button type="primary" @click="applyFilters">应用筛选</n-button>
            <n-button @click="resetFilters">重置</n-button>
          </n-space>
          <n-space align="center">
            <span class="result-count">共 {{ items.length }} 条</span>
            <n-pagination data-testid="log-pagination" v-model:page="page" :page-count="pageCount" :page-size="pageSize" />
          </n-space>
        </div>
        <n-empty v-if="!loading&&!items.length" description="暂无匹配日志"/>
        <template v-else>
          <div ref="topScrollbar" data-testid="log-top-scrollbar" class="top-scrollbar" @scroll="syncTableScroll">
            <div class="top-scrollbar-content" :style="{width:`${tableScrollX}px`}"/>
          </div>
          <n-data-table ref="tableRef" class="log-table" data-testid="log-table" :columns="columns" :data="pagedItems" :loading="loading" :pagination="false" :scroll-x="tableScrollX"/>
        </template>
      </n-card>
    </div>
    <n-modal v-model:show="showDetail" preset="card" title="调用详情" style="width:min(900px,92vw)">
      <n-code :code="JSON.stringify(detail,null,2)" language="json" word-wrap/>
    </n-modal>
  </n-space>
</template>
<script setup lang="ts">
import { computed,h,onMounted,reactive,ref } from 'vue'
import { NAlert,NButton,NCard,NCode,NDataTable,NDatePicker,NEmpty,NH1,NInputNumber,NModal,NPagination,NSelect,NSpace,NTag } from 'naive-ui'
import { getJson } from '../api/client'
import { chinaDatePickerValueToIso, formatChinaDateTime } from '../dateTime'
const items=ref<any[]>([]);const unified=ref<any[]>([]);const providers=ref<any[]>([]);const upstream=ref<any[]>([]);const loading=ref(false);const showDetail=ref(false);const detail=ref<any>()
const page=ref(1);const pageSize=20;const tableScrollX=1640;const tableRef=ref<any>();const topScrollbar=ref<HTMLElement>()
const filters=reactive<any>({range:null,success:null,request_kind:null,unified_model:null,provider_instance_id:null,upstream_model_id:null,inbound_protocol:null,min_cost:null,max_cost:null})
const statusOptions=[{label:'成功',value:'true'},{label:'失败',value:'false'}];const kindOptions=[{label:'主调用',value:'main'},{label:'辅助调用',value:'auxiliary'}];const protocolOptions=['auxiliary','openai_chat','openai_responses','anthropic_messages','gemini_v1beta','embeddings','files','images','audio','moderations','rerank','search','batches','websocket','video','music'].map(value=>({label:value,value}));const unifiedOptions=computed(()=>unified.value.map(x=>({label:x.name,value:x.name})));const providerOptions=computed(()=>providers.value.map(x=>({label:x.name,value:x.id})));const upstreamOptions=computed(()=>upstream.value.map(x=>({label:`${x.provider_name} / ${x.model_id}`,value:x.id})))
const pageCount=computed(()=>Math.max(1,Math.ceil(items.value.length/pageSize)));const pagedItems=computed(()=>items.value.slice((page.value-1)*pageSize,page.value*pageSize))
function query(){const params=new URLSearchParams();for(const key of ['success','request_kind','unified_model','provider_instance_id','upstream_model_id','inbound_protocol','min_cost','max_cost'])if(filters[key]!==null&&filters[key]!==undefined&&filters[key]!=='')params.set(key,String(filters[key]));if(filters.range){params.set('started_after',chinaDatePickerValueToIso(filters.range[0]));params.set('started_before',chinaDatePickerValueToIso(filters.range[1]))}return params.toString()}
async function load(){loading.value=true;try{items.value=await getJson(`/api/admin/logs?${query()}`);page.value=Math.min(page.value,pageCount.value)}finally{loading.value=false}}
async function refresh(){await load()}
async function applyFilters(){page.value=1;await load()}
async function initialize(){[unified.value,providers.value]=await Promise.all([getJson('/api/admin/unified-models'),getJson('/api/admin/provider-instances')]) as any;upstream.value=(await Promise.all(providers.value.map(async p=>(await getJson<any[]>(`/api/admin/provider-instances/${p.id}/upstream-models`)).map(x=>({...x,provider_name:p.name}))))).flat();await load()}
function openDetail(row:any){detail.value=row;showDetail.value=true}
function resetFilters(){Object.assign(filters,{range:null,success:null,request_kind:null,unified_model:null,provider_instance_id:null,upstream_model_id:null,inbound_protocol:null,min_cost:null,max_cost:null});page.value=1;void load()}
function syncTableScroll(event:Event){tableRef.value?.scrollTo?.({left:(event.currentTarget as HTMLElement).scrollLeft})}
const columns:any[]=[{title:'请求 ID',key:'request_id',width:220,ellipsis:{tooltip:true}},{title:'调用类型',key:'request_kind',width:100,render:(r:any)=>h(NTag,{type:r.request_kind==='auxiliary'?'info':'default'},{default:()=>r.request_kind==='auxiliary'?'辅助':'主调用'})},{title:'协议',key:'inbound_protocol',width:150},{title:'供应商',key:'provider_name',width:160,render:(r:any)=>r.provider_name||'-'},{title:'上游模型',key:'upstream_model_name',width:200,ellipsis:{tooltip:true},render:(r:any)=>r.upstream_model_name||'-'},{title:'统一模型',key:'unified_model',width:180},{title:'来源',key:'api_token_name',width:160,ellipsis:{tooltip:true},render:(r:any)=>r.api_token_name||'DeepSeek Harness'},{title:'状态',key:'success',width:80,render:(r:any)=>h(NTag,{type:r.success?'success':'error'},{default:()=>r.success?'成功':'失败'})},{title:'延迟',key:'latency',width:110,render:(r:any)=>`${Number(r.latency_ms||0).toFixed(1)} ms`},{title:'时间（UTC+8）',key:'started_at',width:190,render:(r:any)=>formatChinaDateTime(r.started_at)},{title:'操作',key:'actions',width:80,fixed:'right',render:(r:any)=>h(NButton,{size:'small',onClick:()=>openDetail(r)},{default:()=> '详情'})}]
onMounted(initialize)
</script>
<style scoped>
.log-card-stack{display:flex;flex-direction:column;gap:0}
.log-card-stack :deep(.n-card:first-child){border-bottom-right-radius:0;border-bottom-left-radius:0}
.log-card-stack :deep(.n-card:last-child){margin-top:-1px;border-top-left-radius:0;border-top-right-radius:0}
.filter-grid{display:grid;grid-template-columns:minmax(320px,1.6fr) repeat(3,minmax(170px,1fr));gap:12px;align-items:center}
.filter-grid>*{min-width:0}
.filter-grid :deep(.n-base-selection-placeholder),.filter-grid :deep(.n-input__placeholder){color:#667085!important;opacity:1}
.filter-grid :deep(.n-base-selection),.filter-grid :deep(.n-input){--n-border:1px solid #b8c0cc!important;--n-border-hover:1px solid #18a058!important}
.cost-range{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:8px;align-items:center}
.result-controls{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:10px;flex-wrap:wrap}
.result-count{color:#667085;white-space:nowrap}
.top-scrollbar{height:16px;overflow-x:auto;overflow-y:hidden;margin:0 0 6px}
.top-scrollbar-content{height:1px}
.log-table :deep(.n-scrollbar-rail--horizontal){display:none!important}
@media (max-width:1500px){.filter-grid{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
@media (max-width:680px){.filter-grid{grid-template-columns:1fr}.result-controls{align-items:flex-start;flex-direction:column}}
</style>
