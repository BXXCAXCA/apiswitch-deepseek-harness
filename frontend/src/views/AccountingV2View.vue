<template>
  <n-space vertical size="large"><n-h1>价格与用量</n-h1>
    <n-grid responsive="screen" :cols="'1 s:2 l:4'" :x-gap="16" :y-gap="16"><n-gi><n-card title="总请求"><n-statistic :value="usage.requests||0"/></n-card></n-gi><n-gi><n-card title="成功请求"><n-statistic :value="usage.successful_requests||0"/></n-card></n-gi><n-gi><n-card title="输入 / 输出 Token"><n-statistic :value="`${usage.input_tokens||0} / ${usage.output_tokens||0}`"/></n-card></n-gi><n-gi><n-card title="估算总成本"><n-statistic :value="Number(usage.cost||0)" :precision="6"><template #prefix>$</template></n-statistic></n-card></n-gi></n-grid>
    <n-card title="多维用量聚合"><n-space><n-select v-model:value="dimension" :options="dimensionOptions"/></n-space><n-empty v-if="!grouped.length" description="暂无用量"/><n-data-table v-else :columns="usageColumns" :data="grouped"/></n-card>
    <n-card :title="editing?'手工价格覆盖':'上游模型价格'">
      <n-form v-if="editing" inline><n-form-item label="模型">{{editing.model_id}}</n-form-item><n-form-item label="输入价格"><n-input-number v-model:value="priceForm.input_price" :min="0"/></n-form-item><n-form-item label="输出价格"><n-input-number v-model:value="priceForm.output_price" :min="0"/></n-form-item><n-form-item label="缓存输入"><n-input-number v-model:value="priceForm.cached_input_price" :min="0" clearable/></n-form-item><n-form-item label="币种"><n-input v-model:value="priceForm.currency"/></n-form-item><n-form-item label="来源"><n-input v-model:value="priceForm.pricing_source"/></n-form-item><n-button type="primary" @click="savePrice">保存价格</n-button><n-button @click="editing=null">取消</n-button></n-form>
      <n-empty v-if="!pricing.length" description="尚未配置上游模型"/><n-data-table v-else :columns="priceColumns" :data="pricing" :pagination="{pageSize:20}"/>
    </n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed,h,onMounted,reactive,ref } from 'vue'
import { NButton,NCard,NDataTable,NEmpty,NForm,NFormItem,NGi,NGrid,NH1,NInput,NInputNumber,NSelect,NSpace,NStatistic,useMessage } from 'naive-ui'
import { getJson,patchJson } from '../api/client'
import { formatChinaDateTime } from '../dateTime'
const message=useMessage();const usage=ref<any>({});const pricing=ref<any[]>([]);const providers=ref<any[]>([]);const dimension=ref('by_provider_instance');const editing=ref<any>(null);const priceForm=reactive<any>({input_price:null,output_price:null,cached_input_price:null,currency:'USD',pricing_source:'manual'})
const dimensionOptions=[{label:'按供应商实例',value:'by_provider_instance'},{label:'按上游模型',value:'by_upstream_model'},{label:'按统一模型',value:'by_unified_model'},{label:'按入口协议',value:'by_protocol'}];const grouped=computed(()=>usage.value[dimension.value]||[])
function groupName(row:any){if(dimension.value==='by_provider_instance')return providers.value.find(x=>x.id===row.key)?.name||`#${row.key}`;if(dimension.value==='by_upstream_model')return pricing.value.find(x=>x.id===row.key)?.model_id||`#${row.key}`;return row.key||'-'}
async function load(){[usage.value,pricing.value,providers.value]=await Promise.all([getJson('/api/admin/accounting/usage'),getJson('/api/admin/accounting/pricing'),getJson('/api/admin/provider-instances')]) as any}
function editPrice(row:any){editing.value=row;Object.assign(priceForm,{input_price:row.input_price,output_price:row.output_price,cached_input_price:row.cached_input_price,currency:row.currency||'USD',pricing_source:'manual'})}
async function savePrice(){try{await patchJson(`/api/admin/accounting/pricing/${editing.value.id}`,{...priceForm,pricing_effective_at:new Date().toISOString()});editing.value=null;await load();message.success('手工价格已生效')}catch(error){message.error(String(error))}}
const usageColumns:any[]=[{title:'维度值',key:'key',render:groupName},{title:'请求数',key:'requests'},{title:'输入 Token',key:'input_tokens'},{title:'输出 Token',key:'output_tokens'},{title:'成本',key:'cost',render:(r:any)=>Number(r.cost||0).toFixed(6)}]
const priceColumns:any[]=[{title:'模型 ID',key:'model_id'},{title:'供应商',key:'provider_instance_id',render:(r:any)=>providers.value.find(x=>x.id===r.provider_instance_id)?.name||`#${r.provider_instance_id}`},{title:'输入',key:'input_price'},{title:'输出',key:'output_price'},{title:'缓存输入',key:'cached_input_price'},{title:'币种',key:'currency'},{title:'来源',key:'pricing_source'},{title:'生效时间（UTC+8）',key:'pricing_effective_at',render:(r:any)=>formatChinaDateTime(r.pricing_effective_at)},{title:'操作',key:'actions',render:(r:any)=>h(NButton,{size:'small',onClick:()=>editPrice(r)},{default:()=> '覆盖价格'})}]
onMounted(load)
</script>
