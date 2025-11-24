import bundleManager from '@ohos.bundle.bundleManager';
import { Mapp } from '../Mapp';

/**
 * app工具
 */
export class AppUtils {

    /**
     * 获取版本名称
     */
    public static getVersionName(): string {
        try {
            const context = Mapp.getInstance();
            if (context === null) {
                return "";
            }
            const bundleInfo = bundleManager.getBundleInfoForSelf(bundleManager.BundleFlag.GET_BUNDLE_INFO_WITH_APPLICATION);
            return bundleInfo?.versionName || "";
        } catch (error) {
            return "";
        }
    }

}