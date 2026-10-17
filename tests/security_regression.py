"""
AronaClient 安全回归测试
测试项目: P0 安全审计修复
运行: python tests/security_regression.py
"""
import ast
import os
import sys
import subprocess
import hashlib
from pathlib import Path


def test_no_bom_in_python_files():
    """测试: Python 文件中不应有 UTF-8 BOM"""
    errors = []
    bom = b'\xef\xbb\xbf'
    for root, dirs, files in os.walk('.'):
        # 跳过 .git, __pycache__, .venv
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', '.venv', 'build', 'dist', 'installer')]
        for f in files:
            if f.endswith('.py'):
                path = os.path.join(root, f)
                with open(path, 'rb') as fh:
                    header = fh.read(3)
                if header == bom:
                    errors.append(f'{path}: 含 UTF-8 BOM')
    return errors


def test_no_hardcoded_secrets():
    """测试: 源代码中不应有硬编码密钥"""
    patterns = {
        'sk-* API Key': r'sk-[a-z0-9]{20,}',
        'ghp_ GitHub Token': r'ghp_[a-z0-9]{36}',
        'github_pat_': r'github_pat_[a-z0-9_]{22,}',
        'Private Key': r'BEGIN\s+(RSA|DSA|EC|OPENSSH)\s+PRIVATE\s+KEY',
    }
    errors = []
    for root, dirs, files in os.walk('.'):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', '.venv')]
        for f in files:
            if f.endswith('.py') or f.endswith('.json') or f.endswith('.txt'):
                path = os.path.join(root, f)
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                        content = fh.read()
                    for name, pat in patterns.items():
                        import re
                        if re.search(pat, content):
                            errors.append(f'{path}: 可能含 {name}')
                except:
                    pass
    return errors


def test_config_importable():
    """测试: config.py 可以被 Python 正常导入"""
    errors = []
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('config', 'config.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # 验证关键属性存在
        assert hasattr(mod, 'DEEPSEEK_BASE_URL'), '缺少 DEEPSEEK_BASE_URL'
        assert hasattr(mod, 'DEEPSEEK_API_KEY'), '缺少 DEEPSEEK_API_KEY'
        assert hasattr(mod, 'SYSTEM_PROMPT'), '缺少 SYSTEM_PROMPT'
        assert len(mod.SYSTEM_PROMPT) > 100, 'SYSTEM_PROMPT 过短（可能损坏）'
    except Exception as e:
        errors.append(f'config.py 导入失败: {e}')
    return errors


def test_gitignore_exists():
    """测试: .gitignore 存在且包含必要规则"""
    errors = []
    if not os.path.exists('.gitignore'):
        errors.append('.gitignore 不存在')
        return errors
    with open('.gitignore', 'r', encoding='utf-8') as f:
        content = f.read()
    required = ['.venv', 'build', 'dist', 'data/conversations.json', 'data/memories.json', '.env']
    for r in required:
        if r not in content:
            errors.append(f'.gitignore 缺少规则: {r}')
    return errors


def test_desktop_awareness_auth():
    """测试: 桌面感知有授权门禁"""
    errors = []
    try:
        with open('core/desktop_awareness.py', 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        if 'is_authorized' not in content:
            errors.append('desktop_awareness.py 缺少授权检查')
    except Exception as e:
        errors.append(f'desktop_awareness.py 读取失败: {e}')
    return errors


def main():
    tests = [
        ('BOM 检查', test_no_bom_in_python_files),
        ('硬编码密钥检查', test_no_hardcoded_secrets),
        ('config.py 可导入性', test_config_importable),
        ('.gitignore 完整性', test_gitignore_exists),
        ('桌面感知授权检查', test_desktop_awareness_auth),
    ]
    
    results = []
    for name, test_fn in tests:
        try:
            errors = test_fn()
            if errors:
                results.append((name, 'FAIL', errors))
            else:
                results.append((name, 'PASS', []))
        except Exception as e:
            results.append((name, 'ERROR', [str(e)]))
    
    print('=' * 60)
    print('AronaClient 安全回归测试')
    print('=' * 60)
    print()
    
    passed = 0
    failed = 0
    for name, status, errors in results:
        icon = '✓' if status == 'PASS' else '✗'
        print(f'{icon} {name}: {status}')
        for e in errors:
            print(f'    → {e}')
        if status == 'PASS':
            passed += 1
        else:
            failed += 1
    
    print()
    print(f'结果: {passed} 通过, {failed} 失败')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
