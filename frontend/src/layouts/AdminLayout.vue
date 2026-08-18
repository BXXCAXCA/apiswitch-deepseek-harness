<template>
  <n-layout has-sider style="height:100vh">
    <n-layout-sider
      v-model:collapsed="collapsed"
      bordered
      collapsible
      show-trigger="bar"
      collapse-mode="width"
      :collapsed-width="0"
      :width="240"
      @update:collapsed="saveCollapsed"
    >
      <div class="brand-row">
        <span class="brand">APISwitch Harness</span>
        <n-button quaternary circle size="small" title="隐藏侧边栏" aria-label="隐藏侧边栏" @click="collapsed=true">«</n-button>
      </div>
      <n-menu :value="route.path" :options="menuOptions" @update:value="navigate" />
    </n-layout-sider>
    <div class="main-shell">
      <n-layout-content class="content"><router-view /></n-layout-content>
    </div>
  </n-layout>
</template>

<script setup lang="ts">
import { h, onMounted, ref } from 'vue'
import { NButton, NLayout, NLayoutContent, NLayoutSider, NMenu } from 'naive-ui'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { productNavigation } from '../navigation'

const route = useRoute()
const router = useRouter()
const collapsed = ref(localStorage.getItem('apiswitch.sidebar.collapsed') === '1')
const menuOptions = productNavigation.map(({ label, path }) => ({ label: () => h(RouterLink, { to: path }, { default: () => label }), key: path }))
function navigate(path: string) { router.push(path) }
function saveCollapsed(value: boolean) { localStorage.setItem('apiswitch.sidebar.collapsed', value ? '1' : '0') }
onMounted(()=>{if(window.matchMedia('(max-width:700px)').matches)collapsed.value=true})
</script>

<style scoped>
.brand-row{height:96px;display:flex;align-items:center;justify-content:space-between;padding:0 18px 0 22px;box-sizing:border-box}
.brand{font-size:28px;font-weight:700;white-space:nowrap}
.main-shell{min-width:0;flex:1;height:100vh;overflow:hidden;background:#f5f7f9}
.content{height:100%;overflow-y:auto;padding:24px 30px 40px;box-sizing:border-box}
@media (max-width:700px){.content{padding:16px 12px 28px}}
</style>
