from firestore_client import get_firestore

db = get_firestore()

doc_ref = db.collection("test_collection").document("test_doc")

doc_ref.set({
    "message": "Firestore connection successful"
})

print("Firestore write successful")
