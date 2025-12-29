"""
박교준 수리논술 - 신청 확인 시스템
1. 신청 확인 문자 발송
2. 결제선생 청구서 발송
3. 가격조정 청구서 재발송 (파기 후 재발송)
"""

import gspread
from google.oauth2.service_account import Credentials
from dataclasses import dataclass
from typing import List
import requests
from datetime import datetime, timedelta
import time
import logging
import os
import hashlib
import json

# 스크립트 위치 기준 경로 설정
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(SCRIPT_DIR, "notice.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "sms_apply.log")

# 환경변수에서 설정 로드 (없으면 기본값 사용)
ALIGO_API_KEY = os.environ.get("ALIGO_API_KEY", "v7zkfq6h1oi67mafv7s9wvkmiicm2e3k")
ALIGO_USER_ID = os.environ.get("ALIGO_USER_ID", "plabmaster85")
ALIGO_SENDER = os.environ.get("ALIGO_SENDER", "01084431621")
PAYSSAM_API_KEY = os.environ.get("PAYSSAM_API_KEY", "DLTQLDSNWYRRKQBB")
PAYSSAM_MEMBER = os.environ.get("PAYSSAM_MEMBER", "parkkyojoon0001")
PAYSSAM_MERCHANT = os.environ.get("PAYSSAM_MERCHANT", "parkkyojoon0001")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1jzwafX-L-QatwQUxlv5VnLqYZIZB3GQjRKmTEUp2L3g")

# 로그 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


####---------- 결제선생(PaySsam) API ----------####

@dataclass
class BillResult:
    success: bool
    bill_id: str = ""
    short_url: str = ""
    code: str = ""
    message: str = ""


class PaySsamAPI:
    BASE_URL = "https://erp-api.payssam.kr"
    
    def __init__(self, api_key: str = None, member: str = None, merchant: str = None):
        self.api_key = api_key or PAYSSAM_API_KEY
        self.member = member or PAYSSAM_MEMBER
        self.merchant = merchant or PAYSSAM_MERCHANT
    
    def _generate_hash(self, bill_id: str, phone: str, price: str) -> str:
        data = f"{bill_id},{phone},{price}"
        return hashlib.sha256(data.encode()).hexdigest()
    
    def _generate_bill_id(self, row_num: int, suffix: str = "") -> str:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"{timestamp}{row_num:04d}{suffix}"
    
    def send_bill(self, bill_id: str, product_nm: str, message: str, member_nm: str, phone: str, price: str, expire_dt: str = None, callback_url: str = "https://example.com/callback") -> BillResult:
        if not expire_dt:
            expire_dt = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        
        hash_value = self._generate_hash(bill_id, phone, price)
        
        payload = {
            "apikey": self.api_key,
            "member": self.member,
            "merchant": self.merchant,
            "bill": {
                "bill_id": bill_id,
                "product_nm": product_nm,
                "message": message,
                "member_nm": member_nm,
                "phone": phone,
                "price": price,
                "hash": hash_value,
                "expire_dt": expire_dt,
                "callbackURL": callback_url
            }
        }
        
        try:
            response = requests.post(f"{self.BASE_URL}/if/bill/send", json=payload, headers={"Content-Type": "application/json"})
            result = response.json()
            
            if result.get("code") == "0000":
                return BillResult(success=True, bill_id=result.get("bill_id", bill_id), short_url=result.get("shortURL", ""), code=result.get("code"), message=result.get("msg", "성공"))
            else:
                return BillResult(success=False, bill_id=bill_id, code=result.get("code", "9999"), message=result.get("msg", "알 수 없는 오류"))
        except Exception as e:
            return BillResult(success=False, bill_id=bill_id, message=str(e))
    
    def destroy_bill(self, bill_id: str) -> BillResult:
        """청구서 파기"""
        payload = {
            "apikey": self.api_key,
            "member": self.member,
            "merchant": self.merchant,
            "bill": {
                "bill_id": bill_id
            }
        }
        
        try:
            response = requests.post(f"{self.BASE_URL}/if/bill/destroy", json=payload, headers={"Content-Type": "application/json"})
            result = response.json()
            
            if result.get("code") == "0000":
                return BillResult(success=True, bill_id=bill_id, code=result.get("code"), message=result.get("msg", "파기 성공"))
            else:
                return BillResult(success=False, bill_id=bill_id, code=result.get("code", "9999"), message=result.get("msg", "알 수 없는 오류"))
        except Exception as e:
            return BillResult(success=False, bill_id=bill_id, message=str(e))


####---------- 데이터 클래스 ----------####

@dataclass
class BillItem:
    bill_type: str      # 시트 기록용
    product_nm: str     # 청구서용
    reason: str         # 문자용
    schedule: str       # 개강일
    price: int


@dataclass
class Applicant:
    timestamp: str
    user_type: str
    student_name: str
    parent_phone: str
    student_phone: str
    row_num: int
    surinonseul_regular: str = ""
    surinonseul_trial: str = ""
    suneung_regular: str = ""
    existing_status: str = ""
    existing_sms: str = ""
    existing_bill_sent: str = ""
    existing_bill_id: str = ""
    price_adjustment: str = ""
    
    @property
    def primary_phone(self) -> str:
        phone = self.parent_phone or self.student_phone
        phone = ''.join(c for c in str(phone) if c.isdigit())
        if phone and len(phone) >= 9 and not phone.startswith("0"):
            phone = "0" + phone
        return phone
    
    @property
    def adjustment_amount(self) -> int:
        """가격조정 금액 파싱 (-40000, +20000 등)"""
        if not self.price_adjustment or not self.price_adjustment.strip():
            return 0
        try:
            return int(self.price_adjustment.replace(",", "").replace(" ", ""))
        except ValueError:
            return 0
    
    def _parse_selections(self, raw_data: str, price_online: int, price_offline: int) -> List[BillItem]:
        items = []
        if not raw_data or not raw_data.strip():
            return items
        
        for raw in [s.strip() for s in raw_data.split(",") if s.strip()]:
            if "마감" in raw:
                continue
            parts = raw.replace("ᅵ", "ㅣ").split("ㅣ")
            base = parts[0].strip()
            schedule = "ㅣ".join(parts[1:]).strip() if len(parts) > 1 else ""
            product_nm = f"{base} 원비 안내"
            price = price_offline if "현강" in raw else price_online
            items.append(BillItem(bill_type=base, product_nm=product_nm, reason=base, schedule=schedule, price=price))
        return items
    
    def get_bill_items(self) -> List[BillItem]:
        items = []
        items.extend(self._parse_selections(self.surinonseul_regular, 398000, 838000))
        
        if self.surinonseul_trial and self.surinonseul_trial.strip():
            for raw in [s.strip() for s in self.surinonseul_trial.split(",") if s.strip()]:
                if "마감" in raw:
                    continue
                parts = raw.replace("ᅵ", "ㅣ").split("ㅣ")
                base = parts[0].strip()
                schedule = "ㅣ".join(parts[1:]).strip() if len(parts) > 1 else ""
                product_nm = f"{base} 원비 안내"
                items.append(BillItem(bill_type=base, product_nm=product_nm, reason=base, schedule=schedule, price=20000))
        
        items.extend(self._parse_selections(self.suneung_regular, 280000, 400000))
        return items
    
    def get_pending_bill_items(self) -> List[BillItem]:
        return [item for item in self.get_bill_items() if item.bill_type not in self.existing_bill_id]
    
    def get_pending_sms_items(self) -> List[BillItem]:
        return [item for item in self.get_bill_items() if item.bill_type not in self.existing_sms]
    
    def get_existing_bill_ids(self) -> dict:
        """기존 청구서 ID들을 {bill_type: bill_id} 형태로 반환"""
        result = {}
        if not self.existing_bill_id:
            return result
        for line in self.existing_bill_id.strip().split("\n"):
            # 마지막 공백 기준으로 분리 (bill_type에 공백이 포함될 수 있음)
            parts = line.strip().rsplit(" ", 1)
            if len(parts) >= 2:
                bill_type = parts[0]
                bill_id = parts[1]
                result[bill_type] = bill_id
        return result


@dataclass
class SMSResult:
    success: bool
    msg_id: int = 0
    message: str = ""


####---------- 메인 클래스 ----------####

class ApplyChecker:
    """신청 확인 시스템"""
    
    ALIGO_URL = "https://apis.aligo.in"
    
    COLUMNS = {
        "timestamp": "Timestamp",
        "user_type": "신청자는 어떤 분이신가요?",
        "student_name": "학생 이름",
        "parent_phone": "학부모님 연락처",
        "student_phone": "학생 연락처",
        "surinonseul_regular": '[수리논술] "정규 수업" 신청',
        "surinonseul_trial": '[수리논술] "체험 수업" 신청',
        "suneung_regular": "[수능 수학] 정규 수업 신청",
        "payment_status": "결제 상태",
        "sms_sent": "문자 발송",
        "bill_sent": "청구서 발송",
        "bill_id": "청구서 ID",
        "price_adjustment": "가격조정"
    }
    
    def __init__(self, sheet_id: str = None, sheet_name: str = "수업 신청"):
        self.api_key = ALIGO_API_KEY
        self.user_id = ALIGO_USER_ID
        self.sender = ALIGO_SENDER
        self.payssam = PaySsamAPI()
        
        sheet_id = sheet_id or GOOGLE_SHEET_ID
        
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        
        # 환경변수에서 credentials JSON 로드 (GitHub Actions용)
        google_creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if google_creds_json:
            creds_dict = json.loads(google_creds_json)
            credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        else:
            credentials = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
        
        self.gc = gspread.authorize(credentials)
        self.spreadsheet = self.gc.open_by_key(sheet_id)
        self.sheet = self.spreadsheet.worksheet(sheet_name)
        
        self.col_index = {}
        self._load_column_index()
    
    def _load_column_index(self):
        headers = self.sheet.row_values(1)
        for idx, header in enumerate(headers, 1):
            for key, col_name in self.COLUMNS.items():
                if header.strip() == col_name:
                    self.col_index[key] = idx
        self.col_index.setdefault("payment_status", 13)
        self.col_index.setdefault("sms_sent", 14)
        self.col_index.setdefault("bill_sent", 15)
        self.col_index.setdefault("bill_id", 16)
        self.col_index.setdefault("price_adjustment", 18)  # R열
    
    def _get_cell(self, row: dict, key: str) -> str:
        return str(row.get(self.COLUMNS.get(key, ""), "")).strip()
    
    def get_all_applicants(self) -> List[Applicant]:
        records = self.sheet.get_all_records()
        status_col = self.sheet.col_values(self.col_index["payment_status"])
        sms_col = self.sheet.col_values(self.col_index["sms_sent"])
        bill_sent_col = self.sheet.col_values(self.col_index["bill_sent"])
        bill_id_col = self.sheet.col_values(self.col_index["bill_id"])
        price_adj_col = self.sheet.col_values(self.col_index["price_adjustment"])
        
        applicants = []
        for idx, row in enumerate(records, 2):
            app = Applicant(
                timestamp=self._get_cell(row, "timestamp"),
                user_type=self._get_cell(row, "user_type"),
                student_name=self._get_cell(row, "student_name"),
                parent_phone=self._get_cell(row, "parent_phone"),
                student_phone=self._get_cell(row, "student_phone"),
                row_num=idx,
                surinonseul_regular=self._get_cell(row, "surinonseul_regular"),
                surinonseul_trial=self._get_cell(row, "surinonseul_trial"),
                suneung_regular=self._get_cell(row, "suneung_regular"),
                existing_status=status_col[idx-1] if idx-1 < len(status_col) else "",
                existing_sms=sms_col[idx-1] if idx-1 < len(sms_col) else "",
                existing_bill_sent=bill_sent_col[idx-1] if idx-1 < len(bill_sent_col) else "",
                existing_bill_id=bill_id_col[idx-1] if idx-1 < len(bill_id_col) else "",
                price_adjustment=price_adj_col[idx-1] if idx-1 < len(price_adj_col) else ""
            )
            if app.primary_phone:
                applicants.append(app)
        return applicants
    
    def get_new_applicants(self) -> List[Applicant]:
        return [app for app in self.get_all_applicants() if app.get_pending_sms_items()]
    
    def get_bill_pending_applicants(self) -> List[Applicant]:
        pending = []
        for app in self.get_all_applicants():
            if any(item.bill_type in app.existing_sms for item in app.get_pending_bill_items()):
                pending.append(app)
        return pending
    
    def get_price_adjustment_applicants(self) -> List[Applicant]:
        """가격조정이 필요한 학생 목록 (R열에 값이 있는 학생)"""
        return [app for app in self.get_all_applicants() if app.adjustment_amount != 0]
    
    def _update_cell(self, row: int, col_key: str, value: str):
        if col_key in self.col_index:
            self.sheet.update_cell(row, self.col_index[col_key], value)
    
    def append_sms_record(self, app: Applicant, bill_type: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_sms = f"{bill_type} {now}"
        if app.existing_sms.strip():
            new_sms = f"{app.existing_sms}\n{new_sms}"
        self._update_cell(app.row_num, "sms_sent", new_sms)
        app.existing_sms = new_sms
    
    def append_bill_record(self, app: Applicant, bill_type: str, bill_id: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        new_sent = f"{bill_type} {now}"
        if app.existing_bill_sent.strip():
            new_sent = f"{app.existing_bill_sent}\n{new_sent}"
        self._update_cell(app.row_num, "bill_sent", new_sent)
        
        new_id = f"{bill_type} {bill_id}"
        if app.existing_bill_id.strip():
            new_id = f"{app.existing_bill_id}\n{new_id}"
        self._update_cell(app.row_num, "bill_id", new_id)
        
        app.existing_bill_sent = new_sent
        app.existing_bill_id = new_id
    
    def update_bill_record(self, app: Applicant, bill_type: str, new_bill_id: str):
        """기존 청구서 ID를 새 ID로 교체"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # bill_sent 업데이트 (새 기록 추가)
        new_sent = f"{bill_type}(조정) {now}"
        if app.existing_bill_sent.strip():
            new_sent = f"{app.existing_bill_sent}\n{new_sent}"
        self._update_cell(app.row_num, "bill_sent", new_sent)
        
        # bill_id 업데이트 (기존 ID 교체)
        lines = app.existing_bill_id.strip().split("\n") if app.existing_bill_id.strip() else []
        new_lines = []
        replaced = False
        for line in lines:
            if line.startswith(bill_type + " "):
                new_lines.append(f"{bill_type} {new_bill_id}")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{bill_type} {new_bill_id}")
        
        new_id = "\n".join(new_lines)
        self._update_cell(app.row_num, "bill_id", new_id)
        
        app.existing_bill_sent = new_sent
        app.existing_bill_id = new_id
    
    def clear_price_adjustment(self, app: Applicant):
        """가격조정 셀 비우기 (처리 완료 후)"""
        self._update_cell(app.row_num, "price_adjustment", "")
        app.price_adjustment = ""
    
    def _send_sms(self, phone: str, message: str) -> SMSResult:
        data = {
            "key": self.api_key,
            "user_id": self.user_id,
            "sender": self.sender,
            "receiver": phone,
            "msg": message,
        }
        if len(message.encode('euc-kr', errors='replace')) > 90:
            data["msg_type"] = "LMS"
        
        try:
            response = requests.post(f"{self.ALIGO_URL}/send/", data=data)
            result = response.json()
            if int(result.get("result_code", 0)) > 0:
                return SMSResult(success=True, msg_id=result.get("msg_id", 0), message="발송 성공")
            return SMSResult(success=False, message=result.get("message", "알 수 없는 오류"))
        except Exception as e:
            return SMSResult(success=False, message=str(e))
    
    def send_registration_sms(self, applicants: List[Applicant] = None) -> dict:
        """신청 확인 문자 발송"""
        if applicants is None:
            applicants = self.get_new_applicants()
        
        results = {"success": 0, "fail": 0}
        
        for app in applicants:
            pending_items = app.get_pending_sms_items()
            logger.info(f"[신청문자] {app.student_name} - {len(pending_items)}건 / {app.primary_phone}")
            
            for item in pending_items:
                message = f"""{item.reason} 수업 신청

{app.student_name}님 안녕하세요!!

박교준 선생님의
{item.reason} 수업을
신청해주셔서 감사합니다.

학부모님 카카오톡으로 결제선생이 발송되었습니다.
● 수업 확정을 위해 수강료 납부 부탁드립니다.

※ 납부 확인 즉시,
수업 확정 안내드리겠습니다.
★ 10명 중 9명이 합격한 수업

이제 다음은 {app.student_name}님의 차례입니다."""
                
                logger.info(f"  {item.bill_type} ({item.price:,}원)")
                result = self._send_sms(app.primary_phone, message)
                
                if result.success:
                    results["success"] += 1
                    logger.info(f"    → 문자 발송 성공")
                    self.append_sms_record(app, item.bill_type)
                else:
                    results["fail"] += 1
                    logger.error(f"    → 문자 발송 실패: {result.message}")
                time.sleep(0.5)
        
        return results
    
    def send_bills(self, applicants: List[Applicant] = None) -> dict:
        """청구서 발송"""
        if applicants is None:
            applicants = self.get_bill_pending_applicants()
        
        results = {"success": 0, "fail": 0}
        
        for app in applicants:
            items_to_send = [item for item in app.get_pending_bill_items() if item.bill_type in app.existing_sms]
            if not items_to_send:
                continue
            
            logger.info(f"[청구서발송] {app.student_name} - {len(items_to_send)}건 / {app.primary_phone}")
            
            for i, item in enumerate(items_to_send):
                bill_id = self.payssam._generate_bill_id(app.row_num, f"{i+1:02d}")
                message = f"안녕하세요. {app.student_name}님의 {item.product_nm} 안내드립니다. 감사합니다."
                
                logger.info(f"  {item.bill_type} - {item.price:,}원")
                
                result = self.payssam.send_bill(
                    bill_id=bill_id,
                    product_nm=item.product_nm,
                    message=message,
                    member_nm=app.student_name,
                    phone=app.primary_phone,
                    price=str(item.price)
                )
                
                if result.success:
                    results["success"] += 1
                    logger.info(f"    → 성공 (bill_id: {result.bill_id})")
                    self.append_bill_record(app, item.bill_type, result.bill_id)
                else:
                    results["fail"] += 1
                    logger.error(f"    → 실패: [{result.code}] {result.message}")
                time.sleep(0.5)
        
        return results
    
    def send_adjusted_bills(self, applicants: List[Applicant] = None) -> dict:
        """가격조정 청구서 재발송 (기존 파기 후 새로 발송)"""
        if applicants is None:
            applicants = self.get_price_adjustment_applicants()
        
        results = {"success": 0, "fail": 0, "destroy_success": 0, "destroy_fail": 0}
        
        for app in applicants:
            adjustment = app.adjustment_amount
            if adjustment == 0:
                continue
            
            existing_bills = app.get_existing_bill_ids()
            if not existing_bills:
                logger.warning(f"[가격조정] {app.student_name} - 기존 청구서 없음, 건너뜀")
                continue
            
            logger.info(f"[가격조정] {app.student_name} - 조정금액: {adjustment:+,}원 / {app.primary_phone}")
            
            for bill_type, old_bill_id in existing_bills.items():
                # 원래 가격 찾기
                original_item = next((item for item in app.get_bill_items() if item.bill_type == bill_type), None)
                if not original_item:
                    logger.warning(f"  {bill_type} - 원본 항목 찾을 수 없음")
                    continue
                
                original_price = original_item.price
                new_price = original_price + adjustment
                
                if new_price <= 0:
                    logger.warning(f"  {bill_type} - 조정 후 금액이 0 이하 ({new_price:,}원), 건너뜀")
                    continue
                
                logger.info(f"  {bill_type}: {original_price:,}원 → {new_price:,}원")
                
                # 1. 기존 청구서 파기
                logger.info(f"    파기 중... (bill_id: {old_bill_id})")
                destroy_result = self.payssam.destroy_bill(old_bill_id)
                
                if destroy_result.success:
                    results["destroy_success"] += 1
                    logger.info(f"    → 파기 성공")
                else:
                    results["destroy_fail"] += 1
                    logger.warning(f"    → 파기 실패: [{destroy_result.code}] {destroy_result.message}")
                    # 파기 실패해도 새 청구서는 발송 (기존 것이 이미 결제됐을 수 있음)
                
                time.sleep(0.3)
                
                # 2. 새 청구서 발송
                new_bill_id = self.payssam._generate_bill_id(app.row_num, "A")  # 20자리 이하로
                product_nm = f"{original_item.product_nm} (조정)"
                message = f"안녕하세요. {app.student_name}님의 {product_nm} 안내드립니다. 감사합니다."
                
                logger.info(f"    새 청구서 발송 중... ({new_price:,}원)")
                send_result = self.payssam.send_bill(
                    bill_id=new_bill_id,
                    product_nm=product_nm,
                    message=message,
                    member_nm=app.student_name,
                    phone=app.primary_phone,
                    price=str(new_price)
                )
                
                if send_result.success:
                    results["success"] += 1
                    logger.info(f"    → 발송 성공 (new_bill_id: {send_result.bill_id})")
                    self.update_bill_record(app, bill_type, send_result.bill_id)
                else:
                    results["fail"] += 1
                    logger.error(f"    → 발송 실패: [{send_result.code}] {send_result.message}")
                
                time.sleep(0.5)
            
            # 처리 완료 후 가격조정 셀 비우기
            self.clear_price_adjustment(app)
            logger.info(f"  가격조정 셀 초기화 완료")
        
        return results
    
    def check_and_send(self) -> dict:
        """신청 확인 + 청구서 발송"""
        results = {"sms": None, "bill": None}
        
        try:
            # 1. 문자 발송
            new_applicants = self.get_new_applicants()
            if new_applicants:
                pending_count = sum(len(app.get_pending_sms_items()) for app in new_applicants)
                logger.info(f"📱 문자 발송 대상 {len(new_applicants)}명 ({pending_count}건)")
                results["sms"] = self.send_registration_sms(new_applicants)
            
            self.sheet = self.spreadsheet.worksheet("수업 신청")
            
            # 2. 청구서 발송
            bill_pending = self.get_bill_pending_applicants()
            if bill_pending:
                total_bills = sum(len([i for i in app.get_pending_bill_items() if i.bill_type in app.existing_sms]) for app in bill_pending)
                logger.info(f"📄 청구서 발송 대상 {len(bill_pending)}명 ({total_bills}건)")
                results["bill"] = self.send_bills(bill_pending)
            
        except Exception as e:
            logger.error(f"체크 중 오류: {e}")
            import traceback
            traceback.print_exc()
        
        return results


def 자동실행(check_interval: int = 30):
    checker = ApplyChecker()
    
    logger.info("=" * 50)
    logger.info("🚀 신청 확인 시스템 시작")
    logger.info(f"   체크 주기: {check_interval}초")
    logger.info("   종료: Ctrl+C")
    logger.info("=" * 50)
    
    checker._send_sms(checker.sender, "[박교준 수리논술] 신청 확인 시스템이 시작되었습니다.")
    
    while True:
        try:
            logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 시트 확인 중...")
            results = checker.check_and_send()
            
            sms_cnt = results["sms"]["success"] if results["sms"] else 0
            bill_cnt = results["bill"]["success"] if results["bill"] else 0
            
            if sms_cnt or bill_cnt:
                logger.info(f"처리 완료 - 문자: {sms_cnt}건, 청구서: {bill_cnt}건")
            else:
                logger.info("대기 중인 처리 없음")
            
            time.sleep(check_interval)
            checker.sheet = checker.spreadsheet.worksheet("수업 신청")
            
        except KeyboardInterrupt:
            logger.info("\n신청 확인 시스템 종료")
            break
        except Exception as e:
            logger.error(f"오류 발생: {e}")
            time.sleep(check_interval)


def 가격조정실행():
    """가격조정 청구서 재발송 (수동 실행)"""
    checker = ApplyChecker()
    
    logger.info("=" * 50)
    logger.info("💰 가격조정 청구서 재발송")
    logger.info("=" * 50)
    
    applicants = checker.get_price_adjustment_applicants()
    
    if not applicants:
        logger.info("가격조정 대상자가 없습니다.")
        return
    
    logger.info(f"대상자: {len(applicants)}명")
    for app in applicants:
        logger.info(f"  - {app.student_name}: {app.adjustment_amount:+,}원")
    
    confirm = input("\n진행하시겠습니까? (y/n): ")
    if confirm.lower() != 'y':
        logger.info("취소됨")
        return
    
    results = checker.send_adjusted_bills(applicants)
    
    logger.info("=" * 50)
    logger.info(f"처리 완료")
    logger.info(f"  파기: 성공 {results['destroy_success']}건 / 실패 {results['destroy_fail']}건")
    logger.info(f"  발송: 성공 {results['success']}건 / 실패 {results['fail']}건")
    logger.info("=" * 50)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "auto":
            자동실행(check_interval=30)
        elif sys.argv[1] == "adjust":
            가격조정실행()
        elif sys.argv[1] == "once":
            checker = ApplyChecker()
            logger.info("=" * 50)
            logger.info("📱 신청 확인 (1회 실행)")
            logger.info("=" * 50)
            results = checker.check_and_send()
            sms_cnt = results["sms"]["success"] if results["sms"] else 0
            bill_cnt = results["bill"]["success"] if results["bill"] else 0
            logger.info(f"완료 - 문자: {sms_cnt}건, 청구서: {bill_cnt}건")
        else:
            print(f"알 수 없는 명령: {sys.argv[1]}")
    else:
        print("\n" + "=" * 50)
        print("📱 신청 확인 시스템")
        print("=" * 50)
        print("python apply_checker.py auto     # 자동 실행 (30초 주기)")
        print("python apply_checker.py once     # 1회 실행")
        print("python apply_checker.py adjust   # 가격조정 청구서 재발송")
