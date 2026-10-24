// Jenkins 流水线：Keycloak-QA 自动化测试闭环（P6）
// 依赖插件：Pipeline / Git / Gitee / JUnit / Allure
//
// 运行前需在 Jenkins 里配置：
//   1) Manage Jenkins -> Tools -> Allure Commandline
//      名字填 allure，路径填 D:\Dev\allure-2.43.0（本机实测该版本可用）
//   2) 任务类型二选一：
//      a) Pipeline script from SCM —— SCM 选 Git，填 Gitee 仓库地址与凭据，脚本路径 Jenkinsfile
//      b) Pipeline script —— 直接把本文件内容贴进脚本框（无需 git，先跑通闭环）
//   3) Keycloak 由本机 docker compose 常驻，流水线只做可达性检查，不重复起容器

pipeline {
    agent any

    options {
        // 说明：timestamps() 依赖 Timestamper 插件，本机未装故不用；
        // 如需日志带时间戳，装 Timestamper 后加回 timestamps()
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '20'))
    }

    parameters {
        booleanParam(name: 'RUN_UI',      defaultValue: false, description: '跑 Playwright UI 用例（P4，约 25s）')
        booleanParam(name: 'RUN_PERF',    defaultValue: false, description: '跑 JMeter token 压测（P5）')
        booleanParam(name: 'SYNC_ZENTAO', defaultValue: false, description: '把失败用例同步到禅道（需先在节点上配好禅道环境变量）')
        string(name: 'PERF_THREADS', defaultValue: '10', description: '压测并发线程数')
    }

    environment {
        KC_BASE_URL        = 'http://localhost:8080'
        KC_REALM           = 'student-qa'
        KC_ADMIN_USER      = 'qa-admin'
        KC_ADMIN_PASSWORD  = 'qa-admin-123'
        KC_MASTER_USER     = 'admin'
        KC_MASTER_PASSWORD = 'admin'
        PYTHONUTF8         = '1'
        // 走国内 PyPI 镜像，避免首次构建 pip 安装超时
        PIP_INDEX_URL      = 'https://pypi.tuna.tsinghua.edu.cn/simple'
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    // SCM 模式正常检出；内联脚本模式下 scm 为 null，捕获后跳过，便于先跑通闭环
                    try {
                        checkout scm
                        echo '已从 SCM 检出代码'
                    } catch (err) {
                        echo "跳过 checkout（内联脚本模式）：${err.message}"
                    }
                }
            }
        }

        stage('Python 环境') {
            steps {
                // 清掉上一次构建的报告产物，避免 Allure/JUnit 混入旧结果
                bat '''
                    if exist allure-results rmdir /s /q allure-results
                    if exist junit-api.xml del /q junit-api.xml
                    if exist junit-ui.xml del /q junit-ui.xml
                    if not exist .venv python -m venv .venv
                    call .venv\\Scripts\\activate.bat
                    python -m pip install -q --upgrade pip
                    python -m pip install -q -r requirements.txt
                    playwright install chromium
                '''
            }
        }

        stage('Preflight - Keycloak 可达') {
            steps {
                bat '''
                    call .venv\\Scripts\\activate.bat
                    python -c "import requests,sys; r=requests.get('%KC_BASE_URL%/realms/master/.well-known/openid-configuration',timeout=5); print('keycloak status', r.status_code); sys.exit(0 if r.status_code==200 else 1)"
                '''
            }
        }

        // 接口用例不加 -q，让 Console 里能看到每条用例名，便于定位
        stage('接口用例 P2/P3') {
            steps {
                bat '''
                    call .venv\\Scripts\\activate.bat
                    pytest tests --alluredir=allure-results --junitxml=junit-api.xml
                '''
            }
        }

        stage('UI 用例 P4') {
            when { expression { params.RUN_UI } }
            steps {
                bat '''
                    call .venv\\Scripts\\activate.bat
                    pytest ui_tests --alluredir=allure-results --junitxml=junit-ui.xml
                '''
            }
        }

        stage('压测 P5') {
            when { expression { params.RUN_PERF } }
            steps {
                // -o 报告目录必须不存在，故用线程数+构建号拼唯一目录名
                bat '''
                    set REPORT_DIR=perf\\report-%PERF_THREADS%u-%BUILD_NUMBER%
                    "D:\\Dev\\apache-jmeter-5.6.3\\bin\\jmeter.bat" -n -t perf\\keycloak_token.jmx -l perf\\results-%PERF_THREADS%u-%BUILD_NUMBER%.jtl -e -o %REPORT_DIR% -Jthreads=%PERF_THREADS% -Jramp=5 -Jloops=5
                '''
            }
        }

        stage('同步禅道') {
            when { expression { params.SYNC_ZENTAO } }
            steps {
                // 默认 dry-run；确认要真提交时在节点环境变量里配 ZENTAO_* 并加 --apply
                bat '''
                    call .venv\\Scripts\\activate.bat
                    python ci\\zentao_sync.py --junit junit-api.xml
                '''
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'junit-*.xml'
            allure includeProperties: false, jdk: '', results: [[path: 'allure-results']]
            archiveArtifacts artifacts: 'junit-*.xml, perf/results-*.jtl, perf/report-*/**', allowEmptyArchive: true
        }
        failure {
            echo '构建失败：先看 Console Output 里的 pytest 输出，再看 Allure 报告里的失败详情'
        }
    }
}
