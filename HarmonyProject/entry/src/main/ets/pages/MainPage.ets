import { BasePage } from '../base/BasePage';
import router from '@ohos.router';
import { AppUtils } from '../utils/AppUtils';

@Entry
@Component
struct MainPage extends BasePage {
  // 标题栏控件
  private toolbar: any;
  // 查看日记按钮控件
  private browseButton: any;
  // 创建日记按钮控件
  private createButton: any;
  // 当前版本文本控件
  private versionTextView: any;

  // 版本名称状态变量
  @State versionName: string = '';

  aboutToAppear() {
    this.initView();
    this.setView();
  }

  /**
   * 初始化控件
   */
  private initView(): void {
    // 控件将在build方法中通过id绑定
  }

  /**
   * 设置控件
   */
  private setView(): void {
    // 设置版本信息
    this.versionName = AppUtils.getVersionName();
  }

  /**
   * 查看日记按钮点击事件
   */
  private onBrowseButtonClick(): void {
    // 跳转到日记列表页面
    router.pushUrl({
      url: 'pages/DiaryListPage'
    }).catch((error: Error) => {
      console.error(`跳转失败: ${error.message}`);
    });
  }

  /**
   * 创建日记按钮点击事件
   */
  private onCreateButtonClick(): void {
    // 跳转到日记编辑页面，传递日记ID为0表示新建
    router.pushUrl({
      url: 'pages/DiaryEditPage',
      params: {
        diaryId: 0
      }
    }).catch((error: Error) => {
      console.error(`跳转失败: ${error.message}`);
    });
  }

  build() {
    Column() {
      // AppBarLayout转换为Row
      Row() {
        // Toolbar
        Text(/* TODO_RESOURCE: R.string.app_name */)
          .fontSize(20)
          .fontWeight(FontWeight.Bold)
          .textAlign(TextAlign.Center)
          .layoutWeight(1)
      }
      .width('100%')
      .height(/* TODO_RESOURCE: ?attr/actionBarSize */)
      .backgroundColor(/* TODO_RESOURCE: ?attr/colorPrimary */)

      // ScrollView转换为Scroll
      Scroll() {
        Column() {
          // 查看日记按钮
          Button('查看日记', { type: ButtonType.Normal })
            .id('browse_button')
            .backgroundColor(/* TODO_RESOURCE: @drawable/btn_full */)
            .padding({
              left: 30,
              top: 20,
              right: 30,
              bottom: 20
            })
            .fontSize(26)
            .fontColor(/* TODO_RESOURCE: R.color.pink_700 */)
            .onClick(() => {
              this.onBrowseButtonClick();
            })

          // 创建日记按钮
          Button('创建日记', { type: ButtonType.Normal })
            .id('create_button')
            .margin({ top: 50 })
            .backgroundColor(/* TODO_RESOURCE: @drawable/btn_hollow */)
            .padding({
              left: 30,
              top: 20,
              right: 30,
              bottom: 20
            })
            .fontSize(26)
            .fontColor(/* TODO_RESOURCE: R.color.pink_700 */)
            .onClick(() => {
              this.onCreateButtonClick();
            })
        }
        .width('100%')
        .height('100%')
        .justifyContent(FlexAlign.Center)
        .alignItems(HorizontalAlign.Center)
      }
      .width('100%')
      .layoutWeight(1)
      .scrollable(ScrollDirection.Vertical)

      // 版本信息文本
      Text(`当前版本:v${this.versionName}`)
        .id('version_textView')
        .fontSize(12)
        .fontColor(/* TODO_RESOURCE: R.color.pink_300 */)
        .margin(20)
        .alignSelf(ItemAlign.Center)
    }
    .width('100%')
    .height('100%')
    .backgroundColor(/* TODO_RESOURCE: R.color.pink_200 */)
  }
}