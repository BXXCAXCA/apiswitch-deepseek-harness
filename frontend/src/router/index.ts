import { createRouter, createWebHashHistory } from 'vue-router'
import AdminLayout from '../layouts/AdminLayout.vue'

const DashboardView = () => import('../views/DashboardView.vue')
const ProvidersView = () => import('../views/ProvidersView.vue')
const ModelDiscoveryView = () => import('../views/ModelDiscoveryView.vue')
const UnifiedModelsView = () => import('../views/UnifiedModelsView.vue')
const AuxiliaryModelsView = () => import('../views/AuxiliaryModelsView.vue')
const LogsView = () => import('../views/LogsView.vue')
const AccountingV2View = () => import('../views/AccountingV2View.vue')
const BudgetsView = () => import('../views/BudgetsView.vue')
const SystemSettingsV2View = () => import('../views/SystemSettingsV2View.vue')

export const router = createRouter({
  history: createWebHashHistory(import.meta.env.BASE_URL),
  routes: [{ path: '/', component: AdminLayout, children: [
    { path: '', redirect: '/dashboard' }, { path: 'dashboard', component: DashboardView },
    { path: 'providers', component: ProvidersView }, { path: 'upstream-models', component: ModelDiscoveryView },
    { path: 'unified-models', component: UnifiedModelsView }, { path: 'auxiliary-models', component: AuxiliaryModelsView },
    { path: 'logs', component: LogsView },
    { path: 'accounting', component: AccountingV2View },
    { path: 'budgets', component: BudgetsView },
    { path: 'settings', component: SystemSettingsV2View }
  ] }]
})
