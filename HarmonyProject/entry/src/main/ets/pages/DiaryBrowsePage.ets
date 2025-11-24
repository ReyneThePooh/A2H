import { Diary } from '../bean/Diary';
import { Mapp } from '../Mapp';
import { ToastUtils } from '../utils/ToastUtils';
import router from '@ohos.router';
import promptAction from '@ohos.promptAction';
import { BaseBean } from '../bean/BaseBean';

/**
 * 日记浏览页面
 */
@Component
struct DiaryBrowsePage {
  // 页面常量
  private static readonly EXTRA_DIARY_ID: string = "diaryId";
  private static readonly REQUEST_EDIT: number = 1;

  // 状态变量
  @State diaryId: number = 0; // 日记主键
  @State diary: Diary | null = null; // 日记数据
  @State showError: boolean = false; // 是否显示错误
  @State errorText: string = ""; // 错误文本

  // 控件引用状态
  @State dateText: string = ""; // 日期文本
  @State weekText: string = ""; // 星期文本
  @State weatherText: string = ""; // 天气文本
  @State titleText: string = ""; // 标题文本
  @State contentText: string = ""; // 内容文本

  // 菜单操作状态
  @State showMenuDialog: boolean = false; // 是否显示菜单弹窗
  @State showDeleteConfirm: boolean = false; // 是否显示删除确认弹窗

  aboutToAppear(): void {
    // 获取路由参数
    const params = router.getParams() as Record<string, Object>;
    if (params && params[DiaryBrowsePage.EXTRA_DIARY_ID]) {
      this.diaryId = Number(params[DiaryBrowsePage.EXTRA_DIARY_ID]);
      this.loadDiaryData();
    }
  }

  /**
   * 加载日记数据
   */
  private loadDiaryData(): void {
    // 获取日记详情
    const diaryDataSource = Mapp.getInstance()?.getDiaryDataSource();
    if (diaryDataSource) {
      this.diary = diaryDataSource.selectOne(this.diaryId);
      this.setDiaryView(this.diary);
    }
  }

  /**
   * 设置日记显示
   */
  private setDiaryView(diary: Diary | null): void {
    if (diary === null) {
      this.errorText = "未找到该日记";
      this.showError = true;
    } else {
      this.showError = false;
      // 格式化日期
      const date = diary.getDate();
      this.dateText = this.formatDate(date, "yyyy年MM月dd日");
      this.weekText = this.formatDate(date, "EEEE");
      this.weatherText = diary.getWeather();
      this.titleText = diary.getTitle();
      // 处理内容格式，开头空两格，换行空两格
      const content = "\\t\\t\\t\\t" + diary.getContent().replace(/\\n/g, "\\n\\t\\t\\t\\t");
      this.contentText = content;
    }
  }

  /**
   * 格式化日期
   */
  private formatDate(date: Date, format: string): string {
    // 简化实现，实际项目中应使用更完整的日期格式化
    if (format === "yyyy年MM月dd日") {
      return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
    } else if (format === "EEEE") {
      const weekDays = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];
      return weekDays[date.getDay()];
    }
    return date.toString();
  }

  /**
   * 编辑日记
   */
  private updateDiary(): void {
    // 跳转到日记编辑页面
    router.pushUrl({
      url: 'pages/DiaryEditPage',
      params: {
        diaryId: this.diaryId
      }
    }).then(() => {
      // 页面跳转成功
    }).catch((err: Error) => {
      console.error('跳转失败:', err);
    });
  }

  /**
   * 删除日记
   */
  private deleteDiary(): void {
    const diaryDataSource = Mapp.getInstance()?.getDiaryDataSource();
    if (diaryDataSource) {
      diaryDataSource.deleteDiary(this.diaryId);
      ToastUtils.showShort("删除成功");
      // 返回上一页并传递结果
      router.back({
        url: 'pages/DiaryListPage'
      });
    }
  }

  /**
   * 显示菜单弹窗
   */
  private showMenu(): void {
    this.showMenuDialog = true;
  }

  /**
   * 显示删除确认弹窗
   */
  private showDeleteDialog(): void {
    this.showDeleteConfirm = true;
  }

  /**
   * 处理页面返回结果
   */
  private onPageResult(): void {
    // 刷新数据
    this.loadDiaryData();
  }

  /**
   * 构建页面跳转参数
   */
  public static buildParams(diaryId: number): Record<string, Object> {
    return {
      [DiaryBrowsePage.EXTRA_DIARY_ID]: diaryId
    };
  }

  /**
   * 处理页面返回结果
   */
  onPageShow(): void {
    // 监听页面显示，处理从编辑页面返回的情况
    const params = router.getParams() as Record<string, Object>;
    if (params && params['refresh']) {
      // 如果标记需要刷新，则重新加载数据
      this.loadDiaryData();
    }
  }

  build() {
    Column() {
      // 标题栏
      Column() {
        Row() {
          Image($r('app.media.ic_back'))
            .width(24)
            .height(24)
            .margin({ left: 16 })
            .onClick(() => {
              router.back();
            })

          Text("日记详情")
            .fontSize(20)
            .fontWeight(FontWeight.Bold)
            .layoutWeight(1)
            .textAlign(TextAlign.Center)

          Image($r('app.media.ic_more'))
            .width(24)
            .height(24)
            .margin({ right: 16 })
            .onClick(() => {
              this.showMenu();
            })
        }
        .width('100%')
        .height(56)
        .backgroundColor(/* TODO_RESOURCE: R.color.primary */)
      }

      // 内容区域
      if (this.showError) {
        // 错误显示
        Column() {
          Text(this.errorText)
            .fontSize(18)
            .textAlign(TextAlign.Center)
            .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
        }
        .width('100%')
        .height('100%')
        .justifyContent(FlexAlign.Center)
        .padding(30)

      } else {
        // 日记内容显示
        Scroll() {
          Column() {
            // 标题文本
            Text(this.titleText)
              .fontSize(20)
              .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
              .textAlign(TextAlign.Center)
              .margin({ top: 30, left: 20, right: 20 })

            // 日期、星期、天气信息
            Row() {
              Text(this.dateText)
                .fontSize(14)
                .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)

              Text(this.weekText)
                .fontSize(14)
                .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
                .margin({ left: 20 })

              Text(this.weatherText)
                .fontSize(14)
                .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
                .margin({ left: 20 })
            }
            .justifyContent(FlexAlign.Center)
            .width('100%')
            .margin({ top: 30 })

            // 内容文本
            Text(this.contentText)
              .fontSize(18)
              .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
              .textAlign(TextAlign.Start)
              .margin({ top: 30, left: 20, right: 20, bottom: 30 })
          }
          .width('100%')
        }
        .width('100%')
        .height('100%')
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor(/* TODO_RESOURCE: R.color.pink_200 */)

    // 菜单弹窗
    if (this.showMenuDialog) {
      AlertDialog({
        title: '操作',
        message: '请选择操作',
        primaryButton: {
          value: '编辑',
          action: () => {
            this.showMenuDialog = false;
            this.updateDiary();
          }
        },
        secondaryButton: {
          value: '删除',
          action: () => {
            this.showMenuDialog = false;
            this.showDeleteDialog();
          }
        },
        cancel: () => {
          this.showMenuDialog = false;
        }
      })
    }

    // 删除确认弹窗
    if (this.showDeleteConfirm) {
      AlertDialog({
        title: '提示',
        message: '确定要删除日记吗?',
        primaryButton: {
          value: '确定',
          action: () => {
            this.showDeleteConfirm = false;
            this.deleteDiary();
          }
        },
        secondaryButton: {
          value: '取消',
          action: () => {
            this.showDeleteConfirm = false;
          }
        }
      })
    }
  }
}