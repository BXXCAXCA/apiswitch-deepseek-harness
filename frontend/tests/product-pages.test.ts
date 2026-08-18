import { flushPromises, mount } from '@vue/test-utils'
import { NMessageProvider } from 'naive-ui'
import { describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import ProvidersView from '../src/views/ProvidersView.vue'
import ModelDiscoveryView from '../src/views/ModelDiscoveryView.vue'
import UnifiedModelsView from '../src/views/UnifiedModelsView.vue'
import AuxiliaryModelsView from '../src/views/AuxiliaryModelsView.vue'
import TokensView from '../src/views/TokensView.vue'
import BudgetsView from '../src/views/BudgetsView.vue'
import LogsView from '../src/views/LogsView.vue'
import AgentsV2View from '../src/views/AgentsV2View.vue'
import SystemSettingsV2View from '../src/views/SystemSettingsV2View.vue'
import CapabilityCheckboxGroup from '../src/components/CapabilityCheckboxGroup.vue'
import { inputCapabilityOptions } from '../src/modelCapabilities'
import { deleteJson, getJson, patchJson, postJson } from '../src/api/client'

vi.mock('../src/api/client', () => ({
  getJson: vi.fn(async () => []),
  postJson: vi.fn(),
  patchJson: vi.fn(),
  deleteJson: vi.fn()
}))

function mountWithMessage(component: object) {
  return mount(defineComponent({
    render: () => h(NMessageProvider, null, { default: () => h(component) })
  }))
}

describe('generation two product pages', () => {
  it('persists the gateway switch from system settings', async () => {
    const getMock = vi.mocked(getJson)
    const patchMock = vi.mocked(patchJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/runtime') return { base_url: 'http://127.0.0.1:8080' } as any
      if (url === '/api/admin/settings') return { gateway_enabled: true, preferred_port: 8080, upload_limit_bytes: 20971520 } as any
      if (url === '/api/admin/settings/startup') return { enabled: false, command: null } as any
      return [] as any
    })
    patchMock.mockResolvedValueOnce({ gateway_enabled: false } as any)

    const wrapper = mountWithMessage(SystemSettingsV2View)
    await flushPromises()
    const gatewaySwitch: any = wrapper.findComponent('[data-testid="gateway-switch"]')
    expect(gatewaySwitch.props('value')).toBe(true)
    gatewaySwitch.vm.$emit('update:value', false)
    await flushPromises()

    expect(patchMock).toHaveBeenCalledWith('/api/admin/settings', { gateway_enabled: false })
    expect(gatewaySwitch.props('value')).toBe(false)
    expect(wrapper.text()).toContain('网关已停用')
    wrapper.unmount()
    patchMock.mockClear()
    getMock.mockImplementation(async () => [] as any)
  })

  it('renders the provider core form and its loading-safe empty state', async () => {
    const wrapper = mountWithMessage(ProvidersView)
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('模板目录')
    expect(text).toContain('添加供应商实例')
    expect(text).toContain('API Key')
    expect(text).toContain('自定义请求头')
    expect(text).toContain('尚未添加供应商实例')
  })

  it('declares responsive form grids instead of fixed desktop-only columns', () => {
    const wrapper = mountWithMessage(ProvidersView)
    const responsiveGrid = wrapper.findAllComponents({ name: 'Grid' }).find((grid) => grid.props('responsive') === 'screen')
    expect(responsiveGrid).toBeTruthy()
    expect(responsiveGrid?.props('cols')).toBe('1 m:2')
  })

  it('hides redundant manual protocol templates and keeps the custom template', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/provider-templates') return [
        { key: 'manual', name: '手动供应商 · OpenAI 兼容', protocol_type: 'openai_compatible', verification_status: 'manual' },
        { key: 'manual_anthropic', name: '手动供应商 · Anthropic Messages', protocol_type: 'anthropic_messages', verification_status: 'manual' },
        { key: 'manual_gemini', name: '手动供应商 · Gemini', protocol_type: 'gemini', verification_status: 'manual' },
        { key: 'manual_custom', name: '手动供应商 · 自定义协议', protocol_type: 'custom', verification_status: 'manual' }
      ] as any
      return [] as any
    })

    const wrapper = mountWithMessage(ProvidersView)
    await flushPromises()

    expect(wrapper.find('[data-testid="provider-template-manual"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="provider-template-manual_anthropic"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="provider-template-manual_gemini"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="provider-template-manual_custom"]').exists()).toBe(true)
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('orders the provider catalog for horizontal use with actions first', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/provider-templates'
      ? [{ key: 'openai', name: 'OpenAI', protocol_type: 'openai', region: 'global', base_url: 'https://api.openai.com/v1', verification_status: 'unverified' }]
      : [] as any)
    const wrapper = mountWithMessage(ProvidersView)
    await flushPromises()
    const table: any = wrapper.findComponent('[data-testid="provider-template-table"]')
    expect(table.props('columns').map((column: any) => column.title)).toEqual(['操作', '名称', '协议', '地区', '默认地址'])
    expect(table.props('scrollX')).toBe(1700)
    expect(table.props('columns')[0].fixed).toBe('left')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('starts a new instance when a catalog template is chosen during editing and restores its API path', async () => {
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    const patchMock = vi.mocked(patchJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/provider-templates') return [
        { key: 'openai', name: 'OpenAI', protocol_type: 'openai', base_url: 'https://api.openai.com/v1', verification_status: 'unverified' },
        { key: 'lm_studio', name: 'LM Studio', protocol_type: 'openai_compatible', base_url: 'http://127.0.0.1:1234/v1', verification_status: 'compatible' }
      ] as any
      if (url === '/api/admin/provider-instances') return [{ id: 9, name: 'Existing', template_key: 'openai', protocol_type: 'openai', base_url: 'https://api.openai.com/v1', timeout_seconds: 120, enabled: true }] as any
      return [] as any
    })
    postMock.mockResolvedValue({ id: 10 } as any)
    const wrapper = mountWithMessage(ProvidersView)
    await flushPromises()

    await wrapper.find('[data-testid="provider-edit-9"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('编辑供应商实例')
    await wrapper.find('[data-testid="provider-template-lm_studio"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('添加供应商实例')

    const nameInput: any = wrapper.findComponent('[data-testid="provider-name"]')
    nameInput.vm.$emit('update:value', 'LAN LM Studio')
    const baseInput: any = wrapper.findComponent('[data-testid="provider-base-url"]')
    baseInput.vm.$emit('update:value', 'http://192.168.8.101:1234')
    await wrapper.find('[data-testid="save-provider"]').trigger('click')
    await flushPromises()

    expect(patchMock).not.toHaveBeenCalled()
    expect(postMock).toHaveBeenCalledWith('/api/admin/provider-instances', expect.objectContaining({
      template_key: 'lm_studio',
      name: 'LAN LM Studio',
      base_url: 'http://192.168.8.101:1234/v1'
    }))
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
    postMock.mockImplementation(async () => undefined as any)
  })

  it('uses a fixed checkbox vocabulary for upstream and unified model capabilities', async () => {
    for (const component of [ModelDiscoveryView, UnifiedModelsView, AuxiliaryModelsView]) {
      const wrapper = mountWithMessage(component)
      await flushPromises()
      expect(wrapper.findAllComponents(CapabilityCheckboxGroup).length).toBeGreaterThan(0)
      expect(wrapper.text()).not.toContain('能力（逗号分隔）')
      expect(wrapper.text()).not.toContain('能力覆盖 JSON')
    }
  })

  it('emits selected capabilities from checkboxes instead of accepting free text', async () => {
    const wrapper = mount(CapabilityCheckboxGroup, {
      props: { modelValue: ['text'], options: inputCapabilityOptions }
    })
    const group = wrapper.findComponent({ name: 'CheckboxGroup' })
    group.vm.$emit('update:value', ['text', 'vision'])
    await wrapper.vm.$nextTick()
    const updates = wrapper.emitted('update:modelValue')
    expect(updates?.at(-1)?.[0]).toEqual(['text', 'vision'])
  })

  it('pulls a provider catalog for selection while preserving direct model ID entry', async () => {
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/provider-instances'
      ? [{ id: 7, name: '模拟供应商', protocol_type: 'openai_compatible' }]
      : [] as any)
    postMock.mockImplementation(async (url: string) => url.endsWith('/upstream-models/discover')
      ? { models: [{ model_id: 'remote-a', display_name: 'Remote A', input_capabilities_json: ['text'], output_capabilities_json: ['text'], remote_metadata: { input_modalities: ['text', 'image'] } }] }
      : url.endsWith('/infer-capabilities')
        ? { input_capabilities_json: ['text', 'vision'], output_capabilities_json: ['text'], inference_confidence: 'high', inference_evidence: ['远端目录显式声明模型能力'], requires_confirmation: false }
        : {} as any)

    const wrapper = mountWithMessage(ModelDiscoveryView)
    await flushPromises()
    expect(wrapper.text()).toContain('添加上游模型')
    expect(wrapper.text()).not.toContain('手工添加上游模型')

    await wrapper.find('[data-testid="pull-models"]').trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/admin/provider-instances/7/upstream-models/discover', {})
    const remoteSelect: any = wrapper.findComponent('[data-testid="remote-model-select"]')
    expect(remoteSelect.props('options')).toEqual([{ label: 'Remote A · remote-a', value: 'remote-a' }])
    expect(remoteSelect.props('consistentMenuWidth')).toBe(false)
    expect(typeof remoteSelect.props('renderLabel')).toBe('function')

    remoteSelect.vm.$emit('update:value', 'remote-a')
    await flushPromises()
    const modelIdInput: any = wrapper.findComponent('[data-testid="model-id-input"]')
    expect(modelIdInput.props('value')).toBe('remote-a')
    expect(wrapper.text()).toContain('当前选择：remote-a')
    expect(wrapper.text()).toContain('高置信度')
    expect(wrapper.text()).toContain('远端目录显式声明模型能力')

    modelIdInput.vm.$emit('update:value', 'manual-model-id')
    await flushPromises()
    expect(modelIdInput.props('value')).toBe('manual-model-id')

    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
    postMock.mockImplementation(async () => undefined as any)
  })

  it('provides complete write flows for Claude Code, Langcli, and existing agents', async () => {
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/unified-models'
      ? [{ id: 3, name: 'agent-all', enabled: true, enabled_protocols: ['anthropic_messages', 'openai_chat', 'openai_responses', 'gemini_v1beta'] }]
      : [] as any)
    postMock.mockResolvedValue({ config_path: 'C:/Users/test/.codex/config.toml', content: 'model = "agent-all"', language: 'toml', token_hint: '不保存 Token' } as any)
    const wrapper = mountWithMessage(AgentsV2View)
    await flushPromises()
    for (const label of ['Claude Code', 'Codex', 'OpenCode', '龙虾（OpenClaw）', 'DeepSeek Harness', 'Hermes', 'Gemini CLI', 'Langcli']) expect(wrapper.text()).toContain(label)
    const modelSelect: any = wrapper.findComponent('[data-testid="agent-main-model"]')
    expect(modelSelect.props('options')).toEqual([{ label: 'agent-all', value: 3 }])
    modelSelect.vm.$emit('update:value', 3)
    const modelsSelect: any = wrapper.findComponent('[data-testid="agent-models"]')
    expect(modelsSelect.props('multiple')).toBe(true)
    modelsSelect.vm.$emit('update:value', [3])
    await wrapper.find('[data-testid="agent-preview"]').trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/admin/agents/codex/preview', expect.objectContaining({ main_model_id: 3, model_ids: [3], api_token_mode: 'auto', rotate_api_key: false }))
    expect(wrapper.text()).toContain('C:/Users/test/.codex/config.toml')
    const editor: any = wrapper.findComponent('[data-testid="agent-config-content"]')
    expect(editor.exists()).toBe(true)
    editor.vm.$emit('update:value', 'model = "agent-all"\n# 可编辑')
    await wrapper.find('[data-testid="agent-write"]').trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/admin/agents/codex/write', expect.objectContaining({ content: 'model = "agent-all"\n# 可编辑', model_ids: [3] }))

    const tabs: any = wrapper.findComponent('[data-testid="agent-tabs"]')
    tabs.vm.$emit('update:value', 'claude-code')
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-opus-model"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="agent-api-key-status"]').exists()).toBe(true)
    tabs.vm.$emit('update:value', 'langcli')
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-api-key-status"]').exists()).toBe(true)
    tabs.vm.$emit('update:value', 'deepseek-harness')
    await flushPromises()
    expect(wrapper.find('[data-testid="agent-api-key-status"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('明文 Authorization')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
    postMock.mockImplementation(async () => undefined as any)
  })

  it('lets an Agent select an existing API Key and submits its verified plaintext once', async () => {
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/unified-models') return [{ id: 3, name: 'agent-all', enabled: true, enabled_protocols: ['openai_responses'] }] as any
      if (url === '/api/admin/tokens') return [{ id: 9, name: '共享客户端', prefix: 'ask_shared', scopes: ['gateway:invoke'], enabled: true, unified_model_ids: [3] }] as any
      return [] as any
    })
    postMock.mockResolvedValue({ config_path: 'C:/Users/test/.codex/config.toml', content: 'experimental_bearer_token = "ask_shared_plaintext"', language: 'toml' } as any)

    const wrapper = mountWithMessage(AgentsV2View)
    await flushPromises()
    const mainSelect: any = wrapper.findComponent('[data-testid="agent-main-model"]')
    mainSelect.vm.$emit('update:value', 3)
    const modelsSelect: any = wrapper.findComponent('[data-testid="agent-models"]')
    modelsSelect.vm.$emit('update:value', [3])
    const mode: any = wrapper.findComponent('[data-testid="agent-api-key-mode"]')
    mode.vm.$emit('update:value', 'manual')
    await flushPromises()

    const tokenSelect: any = wrapper.findComponent('[data-testid="agent-existing-api-key"]')
    expect(tokenSelect.props('options')).toEqual([{
      label: '共享客户端 · ask_shared…', value: 9, disabled: false
    }])
    tokenSelect.vm.$emit('update:value', 9)
    await flushPromises()
    const plaintext: any = wrapper.findComponent('[data-testid="agent-existing-api-key-plain"]')
    plaintext.vm.$emit('update:value', 'ask_shared_plaintext')
    await wrapper.find('[data-testid="agent-preview"]').trigger('click')
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/admin/agents/codex/preview', expect.objectContaining({
      main_model_id: 3,
      model_ids: [3],
      api_token_mode: 'manual',
      api_token_id: 9,
      api_token: 'ask_shared_plaintext',
      rotate_api_key: false
    }))
    expect(wrapper.text()).toContain('手动 Key 的模型权限不会被 Agent 配置修改')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
    postMock.mockImplementation(async () => undefined as any)
  })

  it('deletes a saved Agent configuration from the written configurations table', async () => {
    const getMock = vi.mocked(getJson)
    const deleteMock = vi.mocked(deleteJson)
    let agentRows: any[] = [{
      id: 7,
      agent_type: 'opencode',
      config_path: 'C:/Users/test/.config/opencode/opencode.json',
      enabled: true,
      model_ids: [3],
      api_token_mode: 'auto',
      api_token_prefix: 'ask_agent'
    }]
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/unified-models') return [{ id: 3, name: 'agent-all', enabled: true, enabled_protocols: ['openai_chat'] }] as any
      if (url === '/api/admin/agents') return agentRows as any
      return [] as any
    })
    deleteMock.mockImplementation(async () => {
      agentRows = []
      return { deleted: true, api_token_deleted: true, config_file_preserved: true } as any
    })

    const wrapper = mountWithMessage(AgentsV2View)
    await flushPromises()
    expect(wrapper.text()).toContain('OpenCode')
    await wrapper.find('[data-testid="agent-delete-opencode"]').trigger('click')
    await flushPromises()

    expect(deleteMock).toHaveBeenCalledWith('/api/admin/agents/opencode')
    expect(wrapper.text()).toContain('尚未写入 Agent 配置')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
    deleteMock.mockImplementation(async () => undefined as any)
  })

  it('disables Combo strategy outside Combo mode and explains capability overrides', async () => {
    const wrapper = mountWithMessage(UnifiedModelsView)
    await flushPromises()
    const routing: any = wrapper.findComponent('[data-testid="routing-mode"]')
    const strategy: any = wrapper.findComponent('[data-testid="combo-strategy"]')
    expect(strategy.props('disabled')).toBe(false)
    routing.vm.$emit('update:value', 'static')
    await flushPromises()
    expect(strategy.props('disabled')).toBe(true)
    expect(wrapper.text()).toContain('留空表示继承上游模型')
    wrapper.unmount()
  })

  it('shows complete upstream model names in the unified candidate selector', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/unified-models') return [{ id: 3, name: 'client-model', candidates: [] }] as any
      if (url === '/api/admin/provider-instances') return [{ id: 4, name: '长名称供应商' }] as any
      if (url === '/api/admin/provider-instances/4/upstream-models') return [{
        id: 8,
        model_id: 'namespace/extremely-long-upstream-model-name-that-must-remain-visible',
        display_name: '完整模型显示名称'
      }] as any
      return [] as any
    })
    const wrapper = mountWithMessage(UnifiedModelsView)
    await flushPromises()
    const select: any = wrapper.findComponent('[data-testid="candidate-upstream-select"]')
    expect(select.props('consistentMenuWidth')).toBe(false)
    expect(typeof select.props('renderLabel')).toBe('function')
    expect(select.props('options')).toEqual([{
      label: '长名称供应商 / 完整模型显示名称 · namespace/extremely-long-upstream-model-name-that-must-remain-visible (#8)',
      value: 8
    }])
    expect(select.props('multiple')).toBe(true)
    select.vm.$emit('update:value', [8])
    await flushPromises()
    expect(wrapper.text()).toContain('已选 1 个：长名称供应商 / 完整模型显示名称 · namespace/extremely-long-upstream-model-name-that-must-remain-visible (#8)')
    expect(wrapper.find('.model-preview').attributes('title')).toContain('namespace/extremely-long-upstream-model-name-that-must-remain-visible')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('keeps full Prompt and Response logging off by default and lets settings enable it', async () => {
    const getMock = vi.mocked(getJson)
    const patchMock = vi.mocked(patchJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/runtime') return { desktop: true } as any
      if (url === '/api/admin/settings') return { save_full_prompt_response: false } as any
      if (url === '/api/admin/settings/startup') return { enabled: false, command: null } as any
      return [] as any
    })
    patchMock.mockResolvedValueOnce({ save_full_prompt_response: true } as any)

    const wrapper = mountWithMessage(SystemSettingsV2View)
    await flushPromises()
    const contentSwitch: any = wrapper.findComponent('[data-testid="save-log-content-switch"]')
    expect(contentSwitch.props('value')).toBe(false)
    expect(wrapper.text()).toContain('默认关闭')
    contentSwitch.vm.$emit('update:value', true)
    await flushPromises()

    expect(patchMock).toHaveBeenCalledWith('/api/admin/settings', { save_full_prompt_response: true })
    expect(contentSwitch.props('value')).toBe(true)
    expect(wrapper.text()).toContain('已开启完整内容保存')
    expect(wrapper.text()).toContain('二进制响应仅记录类型和大小')
    wrapper.unmount()
    patchMock.mockClear()
    getMock.mockImplementation(async () => [] as any)
  })

  it('bulk-configures selected upstream models from one shared form', async () => {
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/provider-instances') return [{ id: 4, name: '供应商 A', protocol_type: 'openai' }, { id: 5, name: '供应商 B', protocol_type: 'openai_compatible' }] as any
      if (url === '/api/admin/upstream-models') return [
        { id: 8, provider_instance_id: 4, provider_name: '供应商 A', model_id: 'model-a', display_name: '模型 A', input_capabilities_json: ['text'], output_capabilities_json: ['text'], context_window: 32000, max_output_tokens: 4096, input_price: 1, output_price: 2, cached_input_price: 0.5, tags_json: ['old'] },
        { id: 9, provider_instance_id: 5, provider_name: '供应商 B', model_id: 'model-b', display_name: '模型 B', input_capabilities_json: ['text'], output_capabilities_json: ['text'] }
      ] as any
      return [] as any
    })
    postMock.mockResolvedValue({ updated: 2, action: 'configure' } as any)
    const wrapper = mountWithMessage(ModelDiscoveryView)
    await flushPromises()
    const filter: any = wrapper.findComponent('[data-testid="model-list-provider-filter"]')
    expect(filter.props('value')).toBe('all')
    expect(filter.props('options')).toEqual([{ label: '全部供应商', value: 'all' }, { label: '供应商 A', value: 4 }, { label: '供应商 B', value: 5 }])
    const table: any = wrapper.findComponent('[data-testid="upstream-model-table"]')
    expect(table.props('data').map((row:any)=>row.provider_name)).toEqual(['供应商 A','供应商 B'])
    expect(table.props('columns').some((column:any)=>column.title==='供应商')).toBe(true)
    expect(table.props('scrollX')).toBe(1490)
    expect(table.props('columns').find((column:any)=>column.key==='display_name')).toMatchObject({ width: 180, ellipsis: { tooltip: true } })
    expect(table.props('columns').find((column:any)=>column.key==='caps')).toMatchObject({ width: 240, ellipsis: { tooltip: true } })
    expect(table.props('columns').find((column:any)=>column.key==='actions')).toMatchObject({ width: 280, fixed: 'right' })
    const search: any = wrapper.findComponent('[data-testid="model-id-search"]')
    search.vm.$emit('update:value', 'MODEL-B')
    await flushPromises()
    expect(table.props('data').map((row:any)=>row.model_id)).toEqual(['model-b'])
    search.vm.$emit('update:value', '')
    await flushPromises()
    table.vm.$emit('update:checkedRowKeys', [8, 9])
    await flushPromises()
    await wrapper.find('[data-testid="bulk-configure-button"]').trigger('click')
    await flushPromises()
    const panel = wrapper.find('[data-testid="bulk-configure-panel"]')
    expect(panel.exists()).toBe(true)
    const groups = panel.findAllComponents(CapabilityCheckboxGroup)
    groups[0].vm.$emit('update:modelValue', ['text', 'vision'])
    groups[1].vm.$emit('update:modelValue', ['text', 'tools'])
    await wrapper.find('[data-testid="save-bulk-configuration"]').trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/admin/upstream-models/bulk', {
      ids: [8, 9],
      action: 'configure',
      configuration: expect.objectContaining({
        input_capabilities_json: ['text', 'vision'],
        output_capabilities_json: ['text', 'tools'],
        context_window: 32000,
        max_output_tokens: 4096,
        tags_json: ['old']
      })
    })
    wrapper.unmount()
    postMock.mockClear()
    getMock.mockImplementation(async () => [] as any)
  })

  it('adds multiple upstream models to one unified model in a single request', async () => {
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/unified-models') return [{ id: 7, name: 'batch-target', candidates: [] }] as any
      if (url === '/api/admin/provider-instances') return [{ id: 4, name: '供应商' }] as any
      if (url === '/api/admin/provider-instances/4/upstream-models') return [
        { id: 8, model_id: 'model-a', display_name: '模型 A' },
        { id: 9, model_id: 'model-b', display_name: '模型 B' }
      ] as any
      return [] as any
    })
    postMock.mockResolvedValue([] as any)
    const wrapper = mountWithMessage(UnifiedModelsView)
    await flushPromises()
    const select: any = wrapper.findComponent('[data-testid="candidate-upstream-select"]')
    select.vm.$emit('update:value', [8, 9])
    await flushPromises()
    expect(wrapper.text()).toContain('批量添加 2 个候选')
    await wrapper.find('[data-testid="save-candidates"]').trigger('click')
    await flushPromises()
    expect(postMock).toHaveBeenCalledWith('/api/admin/unified-models/7/candidates/bulk', {
      upstream_model_ids: [8, 9],
      weight: 100,
      enabled: true,
      capability_overrides: {}
    })
    wrapper.unmount()
    postMock.mockClear()
    getMock.mockImplementation(async () => [] as any)
  })

  it('filters auxiliary upstream models by selected capabilities', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/auxiliary/settings') return { mode: 'global_pool' } as any
      if (url === '/api/admin/provider-instances') return [{ id: 4, name: '长名称供应商' }] as any
      if (url === '/api/admin/provider-instances/4/upstream-models') return [
        { id: 8, provider_instance_id: 4, model_id: 'namespace/extremely-long-vision-embedding-model-name', display_name: '完整模型显示名称', input_capabilities_json: ['text', 'vision'], output_capabilities_json: ['text', 'embeddings'] },
        { id: 9, provider_instance_id: 4, model_id: 'audio-transcriber', display_name: '音频模型', input_capabilities_json: ['text', 'audio'], output_capabilities_json: ['text'] }
      ] as any
      return [] as any
    })
    const wrapper = mountWithMessage(AuxiliaryModelsView)
    await flushPromises()
    const select: any = wrapper.findComponent('[data-testid="aux-upstream-select"]')
    expect(select.props('consistentMenuWidth')).toBe(false)
    expect(typeof select.props('renderLabel')).toBe('function')
    expect(select.props('options').map((option:any)=>option.value)).toEqual([8])
    select.vm.$emit('update:value', 8)
    await flushPromises()
    expect(wrapper.text()).toContain('当前选择：长名称供应商 / 完整模型显示名称 · namespace/extremely-long-vision-embedding-model-name')
    const capabilityGroup = wrapper.findAllComponents(CapabilityCheckboxGroup)[0]
    capabilityGroup.vm.$emit('update:modelValue', ['audio'])
    await flushPromises()
    expect(capabilityGroup.props('modelValue')).toEqual(['audio'])
    expect(select.props('options').map((option:any)=>option.value)).toEqual([9])
    expect(select.props('value')).toBeNull()
    expect(wrapper.text()).toContain('原上游模型不具备全部所选能力，已自动清除')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('reorders unified candidates by dragging rows instead of editing priority numbers', async () => {
    const getMock = vi.mocked(getJson)
    const patchMock = vi.mocked(patchJson)
    const candidates = [
      { id: 1, upstream_model_id: 101, priority: 1, weight: 100, enabled: true },
      { id: 2, upstream_model_id: 102, priority: 2, weight: 100, enabled: true },
      { id: 3, upstream_model_id: 103, priority: 3, weight: 100, enabled: true }
    ]
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/unified-models') return [{ id: 7, name: 'drag-model', candidates }] as any
      return [] as any
    })
    patchMock.mockResolvedValue([] as any)

    const wrapper = mountWithMessage(UnifiedModelsView)
    await flushPromises()
    expect(wrapper.text()).toContain('拖动候选行即可调整优先级')
    expect(wrapper.findAllComponents({ name: 'FormItem' }).some(item => item.props('label') === '优先级')).toBe(false)
    const table: any = wrapper.findComponent('[data-testid="candidate-priority-table"]')
    const rowProps = table.props('rowProps')
    const dataTransfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
    rowProps(candidates[0]).onDragstart({ dataTransfer })
    rowProps(candidates[2]).onDrop({ preventDefault: vi.fn(), dataTransfer })
    await flushPromises()

    expect(patchMock).toHaveBeenCalledWith('/api/admin/unified-models/7/candidates/reorder', { ids: [2, 3, 1] })
    wrapper.unmount()
    patchMock.mockClear()
    getMock.mockImplementation(async () => [] as any)
  })

  it('reorders auxiliary models and workflows by dragging rows', async () => {
    const getMock = vi.mocked(getJson)
    const patchMock = vi.mocked(patchJson)
    const models = [{ id: 11, priority: 1, enabled: true }, { id: 12, priority: 2, enabled: true }]
    const workflows = [
      { id: 21, priority: 1, workflow_type: 'context_compress', input_capability: 'text', output_capability: 'text', ordered_steps: [], enabled: true },
      { id: 22, priority: 2, workflow_type: 'tool_plan', input_capability: 'text', output_capability: 'text', ordered_steps: [], enabled: true }
    ]
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/auxiliary/settings') return { mode: 'global_pool' } as any
      if (url === '/api/admin/auxiliary/models') return models as any
      if (url === '/api/admin/auxiliary/workflows') return workflows as any
      return [] as any
    })
    patchMock.mockResolvedValue([] as any)

    const wrapper = mountWithMessage(AuxiliaryModelsView)
    await flushPromises()
    expect(wrapper.text()).toContain('拖动辅助模型行即可调整优先级')
    expect(wrapper.text()).toContain('拖动工作流行即可调整执行顺序')
    expect(wrapper.findAllComponents({ name: 'FormItemGi' }).some(item => ['优先级', '工作流顺序'].includes(item.props('label')))).toBe(false)
    const dataTransfer = { effectAllowed: '', dropEffect: '', setData: vi.fn() }
    const modelTable: any = wrapper.findComponent('[data-testid="aux-model-priority-table"]')
    modelTable.props('rowProps')(models[0]).onDragstart({ dataTransfer })
    modelTable.props('rowProps')(models[1]).onDrop({ preventDefault: vi.fn(), dataTransfer })
    const workflowTable: any = wrapper.findComponent('[data-testid="workflow-priority-table"]')
    workflowTable.props('rowProps')(workflows[1]).onDragstart({ dataTransfer })
    workflowTable.props('rowProps')(workflows[0]).onDrop({ preventDefault: vi.fn(), dataTransfer })
    await flushPromises()

    expect(patchMock).toHaveBeenCalledWith('/api/admin/auxiliary/models/reorder', { ids: [12, 11] })
    expect(patchMock).toHaveBeenCalledWith('/api/admin/auxiliary/workflows/reorder', { ids: [22, 21] })
    wrapper.unmount()
    patchMock.mockClear()
    getMock.mockImplementation(async () => [] as any)
  })

  it('offers explicit unified-model authorization when creating a token', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/unified-models'
      ? [{ id: 17, name: 'client-model' }]
      : [] as any)
    const wrapper = mountWithMessage(TokensView)
    await flushPromises()
    const select: any = wrapper.findComponent('[data-testid="token-models"]')
    expect(select.props('options')).toEqual([{ label: 'client-model', value: 17 }])
    expect(wrapper.text()).toContain('未选择时模型不可见且不可调用')
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('supports request-count periods scoped to a provider upstream model', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => {
      if (url === '/api/admin/provider-instances') return [{ id: 4, name: '国内供应商' }] as any
      if (url === '/api/admin/provider-instances/4/upstream-models') return [{ id: 8, model_id: 'model-a', display_name: '模型 A' }] as any
      return [] as any
    })
    const wrapper = mountWithMessage(BudgetsView)
    await flushPromises()

    const billing: any = wrapper.findComponent('[data-testid="budget-billing-mode"]')
    expect(billing.props('options')).toContainEqual({ label: '按调用条数', value: 'request_count' })
    billing.vm.$emit('update:value', 'request_count')
    const scope: any = wrapper.findComponent('[data-testid="budget-scope"]')
    scope.vm.$emit('update:value', 'upstream_model')
    await flushPromises()

    expect(wrapper.text()).toContain('调用条数上限')
    const periodSelect: any = wrapper.findComponent('[data-testid="budget-period"]')
    expect(periodSelect.props('options')).toEqual(expect.arrayContaining([
      { label: '滚动 5 小时', value: 'rolling_5_hours' },
      { label: '自然日（UTC+8）', value: 'calendar_day' },
      { label: '自然周（周一至周日，UTC+8）', value: 'calendar_week' }
    ]))
    const targetSelect: any = wrapper.findComponent('[data-testid="budget-target"]')
    expect(targetSelect.props('options')).toEqual([
      { label: '国内供应商 / 模型 A', value: '8' }
    ])
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('sizes the budget table to the full column width so fixed actions do not overlap', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/budgets'
      ? [{ id: 1, name: '每日预算', billing_mode: 'request_count', period_type: 'calendar_day', scope: 'global', usage_value: 0, limit_value: 50, enforcement_action: 'warn', enabled: true }]
      : [] as any)
    const wrapper = mountWithMessage(BudgetsView)
    await flushPromises()
    const table: any = wrapper.findComponent('[data-testid="budget-table"]')
    expect(table.props('scrollX')).toBe(1770)
    expect(table.props('columns').reduce((total: number, column: any) => total + Number(column.width || 0), 0)).toBe(1770)
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('shows Harness log columns without client-management filters', async () => {
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => {
      if (url.startsWith('/api/admin/logs?')) return [{ request_id: 'req_unit', request_kind: 'auxiliary', parent_request_id: 'req_parent', inbound_protocol: 'auxiliary', provider_name: '供应商 A', upstream_model_name: 'model-a', unified_model: 'stable-a', api_token_id: 7, api_token_name: 'DeepSeek Harness', success: true, latency_ms: 12.3, started_at: '2026-07-18T00:00:00Z' }] as any
      return [] as any
    })
    const wrapper = mountWithMessage(LogsView)
    await flushPromises()
    const table: any = wrapper.findComponent('[data-testid="log-table"]')
    expect(table.props('columns').map((column: any) => column.title)).toEqual(['请求 ID', '调用类型', '协议', '供应商', '上游模型', '统一模型', '来源', '状态', '延迟', '时间（UTC+8）', '操作'])
    expect(table.props('pagination')).toBe(false)
    expect(wrapper.find('[data-testid="log-top-scrollbar"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="log-pagination"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="log-table-controls"]').text()).toContain('应用筛选')
    expect(wrapper.text()).toContain('DeepSeek Harness')
    expect(wrapper.text()).toContain('辅助')
    expect(wrapper.text()).not.toContain('失败阶段')
    expect(table.props('columns').some((column: any) => column.title === '成本')).toBe(false)
    expect(wrapper.find('[data-testid="log-client-filter"]').exists()).toBe(false)
    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('shows and copies the current gateway address from client management', async () => {
    const gatewayUrl = 'http://127.0.0.1:8123'
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const getMock = vi.mocked(getJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/runtime'
      ? { base_url: gatewayUrl } as any
      : [] as any)

    const wrapper = mountWithMessage(TokensView)
    await flushPromises()
    expect(wrapper.text()).toContain(gatewayUrl)
    await wrapper.find('[data-testid="copy-gateway-url"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(gatewayUrl)

    wrapper.unmount()
    getMock.mockImplementation(async () => [] as any)
  })

  it('copies the one-time plaintext token with an explicit button', async () => {
    const plaintext = 'ask_unit_placeholder_token'
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const postMock = vi.mocked(postJson)
    postMock.mockResolvedValueOnce({ token: plaintext } as any)

    const wrapper = mountWithMessage(TokensView)
    await flushPromises()
    const nameInput: any = wrapper.findComponent('[data-testid="token-name"]')
    nameInput.vm.$emit('update:value', '客户端测试')
    await wrapper.find('[data-testid="create-token"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="copy-created-token"]').exists()).toBe(true)

    await wrapper.find('[data-testid="copy-created-token"]').trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith(plaintext)
    wrapper.unmount()
  })

  it('rotates a lost token and exposes the replacement plaintext once', async () => {
    const replacement = 'ask_rotated_unit_placeholder'
    const getMock = vi.mocked(getJson)
    const postMock = vi.mocked(postJson)
    getMock.mockImplementation(async (url: string) => url === '/api/admin/tokens'
      ? [{ id: 9, name: '客户端 Token', prefix: 'ask_old', scopes: ['gateway:invoke'], enabled: true }]
      : [] as any)
    postMock.mockResolvedValueOnce({ id: 9, token: replacement, prefix: 'ask_new' } as any)
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true)

    const wrapper = mountWithMessage(TokensView)
    await flushPromises()
    await wrapper.find('[data-testid="rotate-token-9"]').trigger('click')
    await flushPromises()

    expect(postMock).toHaveBeenCalledWith('/api/admin/tokens/9/rotate', {})
    expect(wrapper.find('[data-testid="copy-created-token"]').exists()).toBe(true)
    expect(wrapper.text()).toContain(replacement)
    wrapper.unmount()
    vi.restoreAllMocks()
    getMock.mockImplementation(async () => [] as any)
  })
})
