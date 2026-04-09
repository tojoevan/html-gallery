# HTMLGallery - HTML 作品展示系统

一个简洁的 HTML 作品展示与分享平台，支持用户注册登录、创建展示页面、实时编辑预览、点赞评论等功能。

## 功能特性

- **用户系统**：注册、登录、修改邮箱、修改密码
- **作品管理**：创建、编辑、删除、发布 HTML 展示页面
- **实时编辑器**：HTML 源码编辑 + 实时预览
- **作品展示**：首页卡片列表、详情页查看
- **社交互动**：点赞、作品评论（需登录）
- **评论审核**：作者可审核评论，显示待审核/已通过/已拒绝状态

## 技术栈

- **后端**：Flask (Python)
- **数据库**：SQLite
- **前端**：原生 HTML + CSS + JavaScript
- **样式**：极简清新风格，使用 Inter 字体

## 项目结构

```
html-gallery/
├── app.py              # Flask 主应用
├── requirements.txt    # 依赖列表
├── venv/               # Python 虚拟环境
├── instance/           # SQLite 数据库
├── static/             # 静态资源
│   ├── css/
│   └── js/
└── templates/          # HTML 模板
    ├── index.html      # 首页（作品列表）
    ├── auth.html       # 登录/注册页
    ├── dashboard.html  # 我的作品页
    ├── editor.html     # 编辑器页
    ├── preview.html    # 作品预览页
    └── settings.html   # 用户设置页
```

## 环境要求

- Python 3.8+
- macOS / Linux / Windows

## 安装步骤

### 1. 克隆项目

```bash
cd /你的工作目录
git clone <项目仓库> html-gallery
cd html-gallery
```

### 2. 创建虚拟环境（推荐）

```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate

# Windows
python -m venv venv
venv\Scripts\activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 启动服务

```bash
python app.py
```

服务启动后访问：http://127.0.0.1:5050

## 使用指南

### 注册与登录

1. 访问首页，点击「注册」按钮
2. 填写用户名、邮箱、密码（需确认密码）
3. 注册成功后自动登录

### 创建作品

1. 登录后点击右上角「+ 新建」按钮
2. 在编辑器中输入标题和 HTML 内容
3. 可选择「公开可见」让作品在首页显示
4. 点击「保存修改」创建作品

### 编辑作品

1. 进入「我的作品」页面
2. 点击作品卡片右下角的编辑图标
3. 修改内容后保存

### 预览作品

1. 在首页或我的作品页面点击作品卡片
2. 进入详情页查看渲染效果

### 评论功能

1. 在作品详情页底部可发表评论（需登录）
2. 评论提交后需作者审核才会显示给其他人
3. 作者可「通过」或「拒绝」待审核评论
4. 评论者可删除自己发表的评论

### 用户设置

1. 登录后点击右上角「设置」链接
2. 可修改邮箱（需验证当前密码）
3. 可修改密码（需输入原密码并确认新密码）

## API 接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 首页 |
| `/auth` | GET | 登录/注册页 |
| `/register` | POST | 注册 |
| `/login` | POST | 登录 |
| `/logout` | GET | 登出 |
| `/dashboard` | GET | 我的作品 |
| `/new` | GET/POST | 新建作品 |
| `/edit/<id>` | GET/POST | 编辑作品 |
| `/delete/<id>` | GET | 删除作品 |
| `/preview/<id>` | GET | 预览作品 |
| `/settings` | GET/POST | 用户设置 |
| `/api/pages` | GET | 获取作品列表 API |
| `/api/like/<page_id>` | POST | 点赞/取消点赞 |
| `/api/comments/<page_id>` | GET/POST | 获取/提交评论 |
| `/api/comments/<page_id>/<action>` | POST | 审核评论（通过/拒绝） |
| `/api/comments/<comment_id>/delete` | POST | 删除评论 |

## 配置说明

- **数据库**：SQLite，存储在 `instance/gallery.db`
- **端口**：默认 5050，可修改 `app.py` 中的 `app.run(port=5050)`
- **调试模式**：默认开启，修改 `app.run(debug=True)` 为 `False` 可关闭

## 注意事项

1. 首次运行会自动创建数据库和表
2. 评论功能需用户登录才能使用
3. 评论发布后需作者审核才会公开显示
4. 作品删除后相关评论也会一并删除

## 故障排查

### 端口被占用

```bash
# 查找占用端口的进程
lsof -i :5050

# 杀掉进程后重新启动
kill <PID>
python app.py
```

### 数据库问题

如需重置数据库，删除 `instance/gallery.db` 后重新启动应用。

## 许可证

MIT License