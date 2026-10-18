"""
AronaClient Security Regression Test
Run: python tests/security_regression.py
"""
import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path


def _get_tracked_files():
    """Get list of git-tracked files."""
    result = subprocess.run(
        ['git', 'ls-tree', '-r', '--name-only', 'HEAD'],
        capture_output=True, text=True, encoding='utf-8'
    )
    return set(result.stdout.strip().split('\n'))


def _get_git_status():
    """Get git ignore/track status for key patterns."""
    result = subprocess.run(
        ['git', 'ls-files'], capture_output=True, text=True, encoding='utf-8'
    )
    tracked = set(result.stdout.strip().split('\n'))

    result2 = subprocess.run(
        ['git', 'check-ignore', '-v', '.env', '.env.local', '.env.example',
         'data/conversations.json', 'data/memories.json', 'data/user/'],
        capture_output=True, text=True, encoding='utf-8'
    )
    ignored_lines = result2.stdout.strip().split('\n') if result2.stdout.strip() else []
    return tracked, ignored_lines


def test_no_bom_in_python_files():
    """测试: git 跟踪的 Python 文件中不应有 UTF-8 BOM"""
    errors = []
    bom = b'\xef\xbb\xbf'
    tracked = _get_tracked_files()
    for path in tracked:
        if not path.endswith('.py'):
            continue
        if not os.path.exists(path):
            continue
        with open(path, 'rb') as fh:
            header = fh.read(3)
        if header == bom:
            errors.append(f'{path}: 含 UTF-8 BOM')
    return errors


def test_no_hardcoded_secrets():
    """测试: 跟踪的文件中不应有硬编码密钥"""
    patterns = {
        'sk-* API Key': r'sk-[a-z0-9]{20,}',
        'ghp_ GitHub Token': r'ghp_[a-z0-9]{36}',
        'github_pat_': r'github_pat_[a-z0-9_]{22,}',
        'Private Key': r'BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY',
    }
    errors = []
    tracked = _get_tracked_files()
    for f in tracked:
        if not f.endswith(('.py', '.json', '.txt', '.env', '.yaml', '.yml', '.toml')):
            continue
        if not os.path.exists(f):
            continue
        try:
            with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()
            for name, pat in patterns.items():
                if re.search(pat, content):
                    errors.append(f'{f}: 可能含 {name}')
        except Exception:
            pass
    return errors


def test_config_importable():
    """测试: config.py 可以被 Python 正常导入"""
    errors = []
    try:
        spec = importlib.util.spec_from_file_location('config', 'config.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, 'DEEPSEEK_BASE_URL'), '缺少 DEEPSEEK_BASE_URL'
        assert hasattr(mod, 'DEEPSEEK_API_KEY'), '缺少 DEEPSEEK_API_KEY'
        assert hasattr(mod, 'SYSTEM_PROMPT'), '缺少 SYSTEM_PROMPT'
        assert len(mod.SYSTEM_PROMPT) > 100, 'SYSTEM_PROMPT 过短（可能损坏）'
    except Exception as e:
        errors.append(f'config.py 导入失败: {e}')
    return errors


def test_gitignore_complete():
    """测试: .gitignore 存在且包含必要规则"""
    errors = []
    if not os.path.exists('.gitignore'):
        errors.append('.gitignore 不存在')
        return errors
    with open('.gitignore', 'r', encoding='utf-8') as f:
        content = f.read()
    required = [
        '.venv', 'build', 'dist',
        'data/conversations.json', 'data/memories.json',
        '.env', 'data/user/',
    ]
    for r in required:
        if r not in content:
            errors.append(f'.gitignore 缺少规则: {r}')
    return errors


def test_git_env_ignore_behavior():
    """测试: Git 正确忽略 .env 等敏感文件，跟踪必要文件"""
    errors = []
    tracked, ignored = _get_git_status()

    # .env 相关文件应被忽略
    for pat in ['.env', '.env.local']:
        result = subprocess.run(
            ['git', 'check-ignore', '-v', pat],
            capture_output=True, text=True, encoding='utf-8'
        )
        if result.returncode != 0:
            errors.append(f'{pat} 应被 .gitignore 忽略但未忽略')

    # .env.example 应被跟踪
    if '.env.example' not in tracked:
        errors.append('.env.example 应被 Git 跟踪但未跟踪')

    # 用户数据应被忽略
    for pat in ['data/conversations.json', 'data/memories.json']:
        result = subprocess.run(
            ['git', 'check-ignore', '-v', pat],
            capture_output=True, text=True, encoding='utf-8'
        )
        if result.returncode != 0 and pat not in tracked:
            pass  # OK if not tracked
        elif pat in tracked:
            errors.append(f'{pat} 不应被 Git 跟踪')

    return errors


def test_desktop_awareness_auth_criteria():
    """测试: 桌面感知有完整的授权判据和拒绝路径"""
    errors = []
    try:
        with open('core/desktop_awareness.py', 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        lines = content.split('\n')

        # 检查授权判据
        has_auth_check = any(
            'check_desktop_vision_authorization' in line
            or 'is_authorized' in line
            for line in lines
        )

        # 检查拒绝路径（return/raise when not authorized）
        has_denial_path = any(
            'return' in line and ('None' in line or '授权' in line or '拒绝' in line)
            or 'PermissionError' in line
            or '跳过' in line
            for line in lines
        )

        # 检查授权门禁调用点
        auth_call_count = sum(1 for line in lines if 'check_desktop_vision_authorization' in line)

        if not has_auth_check:
            errors.append('未找到授权判据检查')
        if not has_denial_path:
            errors.append('未找到授权拒绝路径')
        if auth_call_count == 0:
            errors.append('未找到 check_desktop_vision_authorization 调用')

        # 验证具体拒绝逻辑
        denial_patterns = [
            (r'return\s+#.*[授权拒绝]', '注释型拒绝路径'),
            (r'except\s+PermissionError', '异常捕获拒绝路径'),
            (r'return\s+None', '返回None拒绝路径'),
        ]
        found_denials = []
        for pat, desc in denial_patterns:
            if re.search(pat, content):
                found_denials.append(desc)

        if not found_denials:
            errors.append('未找到具体的授权拒绝实现')
        else:
            errors.append(f'OK: 发现拒绝路径 [{", ".join(found_denials)}]')

    except Exception as e:
        errors.append(f'desktop_awareness.py 读取失败: {e}')
    return errors


def test_screen_capture_auth_gate():
    """测试: 截图模块有授权门禁，拒绝时返回空列表"""
    errors = []
    try:
        with open('computer/screen_capture.py', 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # 检查是否从 desktop_access 导入授权门禁
        has_import = any(kw in content for kw in ['desktop_access', 'note_denied', 'authorization'])
        # 检查拒绝行为（返回空列表）
        has_reject_return = 'return []' in content
        # 检查注释中说明授权要求
        has_note = '需要桌面视觉授权' in content or '未授权时' in content

        issues = []
        if not has_import and not has_note:
            issues.append('未找到授权相关导入或说明')
        if not has_reject_return:
            issues.append('未找到拒绝时的返回空列表逻辑')

        if issues:
            errors.extend(issues)

    except FileNotFoundError:
        errors.append('screen_capture.py 不存在')
    except Exception as e:
        errors.append(f'screen_capture.py 读取失败: {e}')
    return errors


def test_desktop_access_auth_gate():
    """测试: 电脑操作模块有统一授权门禁"""
    errors = []
    try:
        with open('computer/desktop_access.py', 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()

        # 检查统一授权门禁
        has_unified_gate = 'is_authorized' in content
        # 检查默认拒绝
        has_default_deny = 'default' in content.lower() and 'deny' in content.lower()
        # 检查拒绝日志
        has_deny_log = 'note_denied' in content or 'logger' in content.lower()

        if not has_unified_gate:
            errors.append('未找到统一授权门禁 is_authorized')
        if not has_deny_log:
            errors.append('未找到拒绝时的日志记录')

    except FileNotFoundError:
        errors.append('desktop_access.py 不存在')
    except Exception as e:
        errors.append(f'desktop_access.py 读取失败: {e}')
    return errors


def test_no_sensitive_paths_in_code():
    """测试: 跟踪的源文件中不应有硬编码的 Windows 用户路径"""
    errors = []
    tracked = _get_tracked_files()
    for f in tracked:
        if not f.endswith('.py'):
            continue
        if not os.path.exists(f):
            continue
        try:
            with open(f, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()
            matches = re.findall(r'C:\\Users\\[^\s"\'`]{5,}', content)
            if matches:
                errors.append(f'{f}: 含硬编码用户路径 ({len(matches)} 处)')
        except Exception:
            pass
    return errors


def test_system_prompt_quality():
    """测试: SYSTEM_PROMPT 包含完整中文内容"""
    errors = []
    try:
        spec = importlib.util.spec_from_file_location('config', 'config.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        prompt = mod.SYSTEM_PROMPT

        # 检查基本结构
        required_sections = ['你是 Arona', 'Hina', '日常聊天', '最重要']
        for section in required_sections:
            if section not in prompt:
                errors.append(f'SYSTEM_PROMPT 缺少关键段落: {section}')

        # 检查中文可读性
        cn_chars = re.findall(r'[\u4e00-\u9fff]', prompt)
        if len(cn_chars) < 200:
            errors.append(f'SYSTEM_PROMPT 中文字符太少 ({len(cn_chars)})，可能损坏')

        # 检查 mojibake 特征：GBK 字节被误作 UTF-8 后产生的字符
        # 正常中文在 U+4E00-U+9FFF，mojibake 会产生 U+0080-U+02FF 范围的字符
        mojibake_ranges = re.findall(r'[\x80-\u02FF]', prompt)
        if mojibake_ranges:
            errors.append(f'SYSTEM_PROMPT 含 mojibake 字符 ({len(mojibake_ranges)} 处)')

    except Exception as e:
        errors.append(f'SYSTEM_PROMPT 质量检查失败: {e}')
    return errors


def main():
    tests = [
        ('BOM 检查（仅跟踪文件）', test_no_bom_in_python_files),
        ('硬编码密钥检查', test_no_hardcoded_secrets),
        ('config.py 可导入性', test_config_importable),
        ('.gitignore 完整性', test_gitignore_complete),
        ('Git .env 忽略行为', test_git_env_ignore_behavior),
        ('桌面感知授权判据', test_desktop_awareness_auth_criteria),
        ('截图授权门禁', test_screen_capture_auth_gate),
        ('电脑操作授权门禁', test_desktop_access_auth_gate),
        ('无硬编码用户路径', test_no_sensitive_paths_in_code),
        ('SYSTEM_PROMPT 质量', test_system_prompt_quality),
    ]

    results = []
    for name, test_fn in tests:
        try:
            errors = test_fn()
            # 过滤掉 "OK: ..." 这种成功提示
            real_errors = [e for e in errors if not e.startswith('OK:')]
            if real_errors:
                results.append((name, 'FAIL', real_errors))
            else:
                results.append((name, 'PASS', []))
        except Exception as e:
            results.append((name, 'ERROR', [str(e)]))

    print('=' * 60)
    print('AronaClient Security Regression Test')
    print('=' * 60)
    print()

    passed = 0
    failed = 0
    for name, status, errors in results:
        icon = '[OK]' if status == 'PASS' else '[FAIL]'
        print(f'{icon} {name}')
        for e in errors:
            print(f'    -> {e}')
        if status == 'PASS':
            passed += 1
        else:
            failed += 1

    print()
    print(f'Results: {passed} passed, {failed} failed')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
