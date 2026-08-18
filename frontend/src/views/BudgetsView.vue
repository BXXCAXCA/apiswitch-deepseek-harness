<template>
  <n-space vertical size="large">
    <n-h1>预算控制</n-h1>
    <n-alert type="info">
      支持按 Token 成本金额或调用条数控制预算。统计周期可选滚动 5 小时、自然日、自然周和自然月；预算可单独限定到供应商或该供应商的具体上游模型。
    </n-alert>

    <n-card :title="editingId ? '编辑预算' : '创建预算'">
      <n-form>
        <div class="budget-form-grid">
          <n-form-item label="名称"><n-input v-model:value="form.name" placeholder="例如：SenseNova 每日调用额度" /></n-form-item>
          <n-form-item label="计费方式"><n-select data-testid="budget-billing-mode" :value="form.billing_mode" :options="billingOptions" @update:value="changeBillingMode" /></n-form-item>
          <n-form-item label="统计周期"><n-select data-testid="budget-period" v-model:value="form.period_type" :options="periodOptions" /></n-form-item>
          <n-form-item label="作用范围"><n-select data-testid="budget-scope" :value="form.scope" :options="scopeOptions" @update:value="changeScope" /></n-form-item>
          <n-form-item v-if="form.scope !== 'global'" label="作用对象" class="target-field">
            <n-select data-testid="budget-target" v-model:value="form.scope_id" filterable :options="scopeTargetOptions" placeholder="请选择供应商或模型" />
          </n-form-item>
          <n-form-item :label="form.billing_mode === 'request_count' ? '调用条数上限' : '金额上限'">
            <n-input-number data-testid="budget-limit" v-model:value="form.limit_value" :min="0" :precision="form.billing_mode === 'request_count' ? 0 : undefined" style="width:100%" />
          </n-form-item>
          <n-form-item v-if="form.billing_mode === 'token_cost'" label="币种"><n-select v-model:value="form.currency" :options="currencyOptions" /></n-form-item>
          <n-form-item label="告警阈值 %"><n-input-number v-model:value="form.alert_threshold_percent" :min="1" :max="100" style="width:100%" /></n-form-item>
          <n-form-item label="超限动作"><n-select v-model:value="form.enforcement_action" :options="actionOptions" /></n-form-item>
          <n-form-item label="启用"><n-switch v-model:value="form.enabled" /></n-form-item>
        </div>
        <n-space>
          <n-button data-testid="save-budget" type="primary" @click="save">{{ editingId ? '保存修改' : '创建预算' }}</n-button>
          <n-button v-if="editingId" @click="reset">取消</n-button>
        </n-space>
      </n-form>
    </n-card>

    <n-card title="预算列表">
      <n-empty v-if="!items.length" description="尚未创建预算" />
      <n-data-table v-else data-testid="budget-table" :columns="columns" :data="items" :scroll-x="1770" />
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NAlert, NButton, NCard, NDataTable, NEmpty, NForm, NFormItem, NH1, NInput, NInputNumber, NSelect, NSpace, NSwitch, NTag, useMessage } from 'naive-ui'
import { deleteJson, getJson, patchJson, postJson } from '../api/client'
import { formatChinaDateTime } from '../dateTime'

const message = useMessage()
const items = ref<any[]>([])
const unified = ref<any[]>([])
const providers = ref<any[]>([])
const upstream = ref<any[]>([])
const editingId = ref<number | null>(null)
const form = reactive<any>({ name: '', scope: 'global', scope_id: null, billing_mode: 'token_cost', period_type: 'calendar_month', limit_value: 10, currency: 'USD', alert_threshold_percent: 80, enforcement_action: 'warn', enabled: true })

const billingOptions = [{ label: 'Token 成本（金额）', value: 'token_cost' }, { label: '按调用条数', value: 'request_count' }]
const periodOptions = [{ label: '滚动 5 小时', value: 'rolling_5_hours' }, { label: '自然日（UTC+8）', value: 'calendar_day' }, { label: '自然周（周一至周日，UTC+8）', value: 'calendar_week' }, { label: '自然月（UTC+8）', value: 'calendar_month' }]
const scopeOptions = [{ label: '全局', value: 'global' }, { label: '统一模型', value: 'unified_model' }, { label: '供应商', value: 'provider_instance' }, { label: '供应商的上游模型', value: 'upstream_model' }]
const actionOptions = [{ label: '仅告警', value: 'warn' }, { label: '拒绝请求', value: 'reject' }, { label: '回退免费候选', value: 'fallback_to_free' }, { label: '回退最低价候选', value: 'fallback_to_cheapest' }]
const currencyOptions = ['USD', 'CNY', 'EUR'].map(value => ({ label: value, value }))

const scopeTargetOptions = computed(() => {
  if (form.scope === 'unified_model') return unified.value.map(x => ({ label: x.name, value: String(x.id) }))
  if (form.scope === 'provider_instance') return providers.value.map(x => ({ label: x.name, value: String(x.id) }))
  if (form.scope === 'upstream_model') return upstream.value.map(x => ({ label: `${x.provider_name} / ${x.display_name || x.model_id}`, value: String(x.id) }))
  return []
})

async function load() {
  const [budgetRows, unifiedRows, providerRows] = await Promise.all([getJson('/api/admin/budgets'), getJson('/api/admin/unified-models'), getJson('/api/admin/provider-instances')]) as any[]
  items.value = budgetRows
  unified.value = unifiedRows
  providers.value = providerRows
  upstream.value = (await Promise.all(providerRows.map(async (provider: any) => (await getJson<any[]>(`/api/admin/provider-instances/${provider.id}/upstream-models`)).map(model => ({ ...model, provider_name: provider.name }))))).flat()
}

function reset() {
  editingId.value = null
  Object.assign(form, { name: '', scope: 'global', scope_id: null, billing_mode: 'token_cost', period_type: 'calendar_month', limit_value: 10, currency: 'USD', alert_threshold_percent: 80, enforcement_action: 'warn', enabled: true })
}

function changeScope(value: string) {
  form.scope = value
  form.scope_id = null
}

function changeBillingMode(value: string) {
  form.billing_mode = value
  form.limit_value = value === 'request_count' ? 100 : 10
}

function edit(row: any) {
  editingId.value = row.id
  Object.assign(form, { name: row.name, scope: row.scope, scope_id: row.scope_id, billing_mode: row.billing_mode || 'token_cost', period_type: row.period_type || 'calendar_month', limit_value: row.limit_value, currency: row.currency || 'USD', alert_threshold_percent: row.alert_threshold_percent || 80, enforcement_action: row.enforcement_action, enabled: row.enabled })
}

async function save() {
  if (!form.name.trim() || (form.scope !== 'global' && !form.scope_id)) return message.warning('请填写名称并选择作用对象')
  if (form.limit_value === null || form.limit_value === undefined) return message.warning('请填写预算上限')
  const payload = { ...form, name: form.name.trim(), scope_id: form.scope === 'global' ? null : String(form.scope_id) }
  try {
    if (editingId.value) await patchJson(`/api/admin/budgets/${editingId.value}`, payload)
    else await postJson('/api/admin/budgets', payload)
    reset()
    await load()
  } catch (error) {
    message.error(String(error))
  }
}

async function toggle(row: any) { await patchJson(`/api/admin/budgets/${row.id}`, { enabled: !row.enabled }); await load() }
async function remove(row: any) { await deleteJson(`/api/admin/budgets/${row.id}`); await load() }

const optionLabel = (options: any[], value: string) => options.find(x => x.value === value)?.label || value
const targetLabel = (row: any) => {
  if (row.scope === 'global') return '全部请求'
  if (row.scope === 'unified_model') return unified.value.find(x => String(x.id) === String(row.scope_id))?.name || `统一模型 #${row.scope_id}`
  if (row.scope === 'provider_instance') return providers.value.find(x => String(x.id) === String(row.scope_id))?.name || `供应商 #${row.scope_id}`
  if (row.scope === 'upstream_model') {
    const model = upstream.value.find(x => String(x.id) === String(row.scope_id))
    return model ? `${model.provider_name} / ${model.display_name || model.model_id}` : `上游模型 #${row.scope_id}`
  }
  return row.scope_id || '-'
}
const usageLabel = (row: any) => row.billing_mode === 'request_count' ? `${row.usage_value || 0} / ${row.limit_value ?? '∞'} 条` : `${Number(row.usage_value || 0).toFixed(6)} / ${row.limit_value ?? '∞'} ${row.currency || 'USD'}`

const columns: any[] = [
  { title: '名称', key: 'name', width: 180, fixed: 'left' },
  { title: '计费方式', key: 'billing_mode', width: 140, render: (row: any) => optionLabel(billingOptions, row.billing_mode) },
  { title: '统计周期', key: 'period_type', width: 210, render: (row: any) => optionLabel(periodOptions, row.period_type) },
  { title: '作用范围', key: 'scope', width: 150, render: (row: any) => optionLabel(scopeOptions, row.scope) },
  { title: '作用对象', key: 'scope_id', width: 250, ellipsis: { tooltip: true }, render: targetLabel },
  { title: '已用 / 上限', key: 'usage', width: 200, render: usageLabel },
  { title: '周期截止（UTC+8）', key: 'period_ends_at', width: 190, render: (row: any) => row.period_ends_at ? formatChinaDateTime(row.period_ends_at) : '-' },
  { title: '动作', key: 'enforcement_action', width: 150, render: (row: any) => optionLabel(actionOptions, row.enforcement_action) },
  { title: '状态', key: 'enabled', width: 90, render: (row: any) => h(NTag, { type: row.enabled ? 'success' : 'default' }, { default: () => row.enabled ? '启用' : '停用' }) },
  { title: '操作', key: 'actions', width: 210, fixed: 'right', render: (row: any) => h(NSpace, { wrap: false }, { default: () => [h(NButton, { size: 'small', onClick: () => edit(row) }, { default: () => '编辑' }), h(NButton, { size: 'small', onClick: () => toggle(row) }, { default: () => row.enabled ? '停用' : '启用' }), h(NButton, { size: 'small', type: 'error', onClick: () => remove(row) }, { default: () => '删除' })] }) },
]

onMounted(load)
</script>

<style scoped>
.budget-form-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 16px}
.target-field{grid-column:span 2}
@media (max-width:1400px){.budget-form-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:720px){.budget-form-grid{grid-template-columns:1fr}.target-field{grid-column:auto}}
</style>
