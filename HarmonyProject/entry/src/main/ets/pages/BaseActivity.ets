import { UIAbility } from '@kit.AbilityKit';
import { router } from '@kit.ArkUI';

/**
 * 页面的基础类
 */
export class BaseActivity extends UIAbility {

    /**
     * 设置菜单点击
     */
    onOptionsItemSelected(itemId: number): boolean {
        // 设置标题栏的返回键点击事件
        if (itemId === /* TODO_RESOURCE: android.R.id.home */) {
            // 页面返回
            this.onBackPressed();
        }
        return true;
    }

    /**
     * 处理返回导航
     */
    onBackPressed(): void {
        // 使用HarmonyOS的路由机制返回上一页
        router.back();
    }

}