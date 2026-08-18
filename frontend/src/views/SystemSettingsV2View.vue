<template>
  <n-space vertical size="large">
    <n-h1>系统设置</n-h1>
    <n-card title="Harness 插件运行信息"><n-descriptions label-placement="left" :column="1"><n-descriptions-item label="当前网关">{{ runtime.base_url }}</n-descriptions-item><n-descriptions-item label="监听">{{ runtime.listen_host }}:{{ runtime.port }}</n-descriptions-item><n-descriptions-item label="数据目录">{{ runtime.data_directory }}</n-descriptions-item><n-descriptions-item label="版本 / Schema">{{ runtime.version }} / {{ runtime.schema_generation }}</n-descriptions-item><n-descriptions-item label="主密钥">{{ runtime.master_key_status }}</n-descriptions-item><n-descriptions-item label="系统">{{ runtime.platform }}</n-descriptions-item></n-descriptions></n-card>
    <n-card title="网关服务">
      <n-space vertical>
        <n-space align="center">
          <n-switch data-testid="gateway-switch" :value="gateway.enabled" :loading="gateway.saving" @update:value="saveGateway" />
          <n-text>{{ gateway.enabled ? '网关已开启' : '网关已停用' }}</n-text>
        </n-space>
        <n-text depth="3">关闭后所有模型调用协议将停止接收请求，管理界面、系统设置和数据不会受影响。</n-text>
      </n-space>
    </n-card>
    <n-card title="运行参数"><n-form inline><n-form-item label="首选端口"><n-input-number v-model:value="general.preferred_port" :min="1" :max="65535" /></n-form-item><n-form-item label="上传限制（bytes）"><n-input-number v-model:value="general.upload_limit_bytes" :min="1" /></n-form-item><n-button type="primary" @click="saveGeneral">保存</n-button></n-form></n-card>
    <n-card title="日志隐私"><n-space vertical><n-switch data-testid="save-log-content-switch" :value="logPrivacy.enabled" :loading="logPrivacy.saving" @update:value="saveLogContent">保存完整 Prompt / Response</n-switch><n-text depth="3">默认关闭。开启后仅对后续调用保存规范化完整请求与响应，可能包含敏感内容；关闭不会删除此前已保存的日志。</n-text><n-alert v-if="logPrivacy.enabled" type="warning">已开启完整内容保存。文本、推理和工具调用会写入本地数据库；二进制响应仅记录类型和大小。</n-alert></n-space></n-card>
    <n-card title="全量加密备份与恢复">
      <n-space vertical>
        <n-form inline><n-form-item label="独立备份密码"><n-input v-model:value="backup.password" type="password" show-password-on="click" /></n-form-item><n-form-item label="输出路径（可选）"><n-input v-model:value="backup.destination" /></n-form-item><n-button @click="createBackup">创建备份</n-button></n-form>
        <n-alert v-if="backupResult" type="success">{{ backupResult }}</n-alert>
        <n-form inline><n-form-item label="归档路径"><n-input v-model:value="restore.archive_path" /></n-form-item><n-form-item label="备份密码"><n-input v-model:value="restore.password" type="password" /></n-form-item><n-form-item label="冲突策略"><n-select v-model:value="restore.conflict_strategy" :options="conflictOptions"/></n-form-item><n-checkbox v-model:checked="restore.confirm">确认替换本地数据</n-checkbox><n-button @click="previewBackup">差异预览</n-button><n-button type="error" @click="restoreBackup">验证并恢复</n-button></n-form><n-code v-if="restorePreview" :code="JSON.stringify(restorePreview,null,2)" language="json" word-wrap/>
      </n-space>
    </n-card>
    <n-card title="WebDAV 全量备份">
      <n-form inline><n-form-item label="名称"><n-input v-model:value="davForm.name" /></n-form-item><n-form-item label="URL"><n-input v-model:value="davForm.url" /></n-form-item><n-form-item label="用户名"><n-input v-model:value="davForm.username" /></n-form-item><n-form-item label="WebDAV 密码"><n-input v-model:value="davForm.password" type="password" /></n-form-item><n-form-item label="独立备份密码"><n-input v-model:value="davForm.backup_password" type="password" /></n-form-item><n-button type="primary" @click="addDav">添加</n-button></n-form>
      <n-data-table :columns="davColumns" :data="davProfiles" />
      <n-form inline><n-form-item label="配置"><n-select v-model:value="transfer.profile_id" :options="davOptions" /></n-form-item><n-form-item label="远端归档路径"><n-input v-model:value="transfer.remote_path" placeholder="backups/apiswitch.apsbak" /></n-form-item><n-button @click="uploadDav">加密并上传</n-button><n-button @click="downloadDav">下载（不自动恢复）</n-button></n-form>
      <n-code v-if="remoteArchives.length" :code="JSON.stringify(remoteArchives,null,2)" language="json" word-wrap/>
    </n-card>
    <n-card title="WebDAV 同步日志"><n-data-table :columns="davLogColumns" :data="davLogs" :pagination="{pageSize:10}"/></n-card>
    <n-card title="系统诊断（不含密钥、数据库和请求内容）"><n-button @click="loadDiagnostics">生成诊断摘要</n-button><n-code v-if="diagnostic" :code="JSON.stringify(diagnostic,null,2)" language="json" word-wrap/></n-card>
  </n-space>
</template>
<script setup lang="ts">
import { computed, h, onMounted, reactive, ref } from 'vue'
import { NAlert, NButton, NCard, NCheckbox, NCode, NDataTable, NDescriptions, NDescriptionsItem, NForm, NFormItem, NGi, NGrid, NH1, NInput, NInputNumber, NSelect, NSpace, NSwitch, NText, useMessage } from 'naive-ui'
import { deleteJson, getJson, patchJson, postJson } from '../api/client'
import { formatChinaDateTime } from '../dateTime'
const message=useMessage();const runtime=ref<any>({});const gateway=reactive({enabled:true,saving:false});const logPrivacy=reactive({enabled:false,saving:false});const general=reactive<any>({preferred_port:8080,upload_limit_bytes:20971520});const backup=reactive({password:'',destination:''});const restore=reactive<any>({archive_path:'',password:'',confirm:false,conflict_strategy:'abort'});const backupResult=ref('');const restorePreview=ref<any>();const conflictOptions=[{label:'检测到冲突时中止',value:'abort'},{label:'验证后替换本地数据',value:'replace_local'}]
const davProfiles=ref<any[]>([]);const davLogs=ref<any[]>([]);const remoteArchives=ref<any[]>([]);const diagnostic=ref<any>();const davForm=reactive({name:'',url:'',username:'',password:'',backup_password:''});const transfer=reactive<any>({profile_id:null,remote_path:'backups/apiswitch.apsbak'});const davOptions=computed(()=>davProfiles.value.map(x=>({label:x.name,value:x.id})))
async function load(){const [r,s,dav,logs]:any=await Promise.all([getJson('/api/admin/runtime'),getJson('/api/admin/settings'),getJson('/api/admin/webdav/profiles'),getJson('/api/admin/webdav/logs')]);runtime.value=r;gateway.enabled=s.gateway_enabled!==false;logPrivacy.enabled=s.save_full_prompt_response===true;Object.assign(general,{preferred_port:s.preferred_port??8080,upload_limit_bytes:s.upload_limit_bytes??20971520});davProfiles.value=dav;davLogs.value=logs}
async function saveGateway(value:boolean){gateway.saving=true;try{const settings:any=await patchJson('/api/admin/settings',{gateway_enabled:value});gateway.enabled=settings.gateway_enabled!==false;message.success(gateway.enabled?'网关已开启':'网关已停用')}catch(error){message.error(String(error))}finally{gateway.saving=false}}
async function saveGeneral(){await patchJson('/api/admin/settings',general);message.success('运行参数已保存，端口在下次启动时生效')}
async function saveLogContent(enabled:boolean){logPrivacy.saving=true;try{const settings:any=await patchJson('/api/admin/settings',{save_full_prompt_response:enabled});logPrivacy.enabled=settings.save_full_prompt_response===true;message.success(logPrivacy.enabled?'已开启完整 Prompt / Response 保存':'已关闭完整 Prompt / Response 保存')}catch(error){message.error(String(error))}finally{logPrivacy.saving=false}}
async function createBackup(){try{const result:any=await postJson('/api/admin/database/backup',{backup_password:backup.password,destination:backup.destination||undefined});backupResult.value=`已创建：${result.path}（SHA-256 ${result.sha256}）`}catch(error){message.error(String(error))}}
async function previewBackup(){try{restorePreview.value=await postJson('/api/admin/webdav/preview',{archive_path:restore.archive_path,backup_password:restore.password})}catch(error){message.error(String(error))}}
async function restoreBackup(){try{await postJson('/api/admin/webdav/restore',{archive_path:restore.archive_path,backup_password:restore.password,confirm:restore.confirm,conflict_strategy:restore.conflict_strategy,profile_id:transfer.profile_id});message.success('恢复完成，请重启 APISwitch')}catch(error){message.error(String(error))}}
async function addDav(){try{await postJson('/api/admin/webdav/profiles',davForm);Object.assign(davForm,{name:'',url:'',username:'',password:'',backup_password:''});await load()}catch(error){message.error(String(error))}}
async function testDav(row:any){try{await postJson(`/api/admin/webdav/profiles/${row.id}/test`,{});message.success('WebDAV 连接成功')}catch(error){message.error(String(error))}}
async function removeDav(row:any){await deleteJson(`/api/admin/webdav/profiles/${row.id}`);await load()}
async function listDav(row:any){try{remoteArchives.value=await getJson(`/api/admin/webdav/profiles/${row.id}/archives`);transfer.profile_id=row.id}catch(error){message.error(String(error))}}
async function uploadDav(){if(!transfer.profile_id)return message.warning('请选择 WebDAV 配置');try{await postJson('/api/admin/webdav/upload',transfer);message.success('加密归档已上传')}catch(error){message.error(String(error))}}
async function downloadDav(){if(!transfer.profile_id)return message.warning('请选择 WebDAV 配置');try{const result:any=await postJson('/api/admin/webdav/download',transfer);restore.archive_path=result.archive_path;message.success('已下载；必须验证密码并显式恢复')}catch(error){message.error(String(error))}}
async function loadDiagnostics(){diagnostic.value=await getJson('/api/admin/diagnostics')}
const davColumns:any[]=[{title:'名称',key:'name'},{title:'URL',key:'url'},{title:'用户',key:'username'},{title:'WebDAV 密码',key:'password_configured',render:(r:any)=>r.password_configured?'已配置':'未配置'},{title:'备份密码',key:'backup_password_configured',render:(r:any)=>r.backup_password_configured?'已配置':'未配置'},{title:'操作',key:'actions',render:(r:any)=>h(NSpace,{}, {default:()=>[h(NButton,{size:'small',onClick:()=>testDav(r)},{default:()=> '测试'}),h(NButton,{size:'small',onClick:()=>listDav(r)},{default:()=> '远端列表'}),h(NButton,{size:'small',type:'error',onClick:()=>removeDav(r)},{default:()=> '删除'})]})}]
const davLogColumns:any[]=[{title:'时间（UTC+8）',key:'created_at',render:(r:any)=>formatChinaDateTime(r.created_at)},{title:'方向',key:'direction'},{title:'远端版本',key:'remote_version'},{title:'冲突决定',key:'conflict_decision'},{title:'结果',key:'success',render:(r:any)=>r.success?'成功':'失败'},{title:'错误',key:'error_message'}]
onMounted(load)
</script>
